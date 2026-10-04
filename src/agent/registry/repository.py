"""Data access layer for the tool registry."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import cast, delete, desc, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from agent.models.tool import ToolManifest, ToolRecord, summarize_schema
from agent.registry.tables import (
    Base,
    Message,
    SandboxSessionRecord,
    Session,
    Tool,
    ToolRun,
    ToolTombstone,
)

log = logging.getLogger(__name__)

QUARANTINE_AFTER = 3

#: key written into ``tool_runs.args_redacted`` when a hard delete unlinks a run row,
#: so an orphaned ledger entry still says which tool it belongs to
LEDGER_REF_KEY = "tool_ref"


def build_search_text(name: str, description: str, when_to_use: str, tags: Sequence[str]) -> str:
    return " ".join(part for part in (name.replace("_", " "), description, when_to_use, " ".join(tags)) if part)


def record_from_row(row: Tool) -> ToolRecord:
    return ToolRecord(
        id=row.id,
        name=row.name,
        version=row.version,
        status=row.status,  # type: ignore[arg-type]
        tier=row.tier,  # type: ignore[arg-type]
        executor=row.executor,  # type: ignore[arg-type]
        description=row.description,
        when_to_use=row.when_to_use,
        tags=list(row.tags or []),
        params_schema=row.params_schema or {},
        permissions=list(row.permissions or []),
        timeout_s=row.timeout_s,
        entrypoint=row.entrypoint,
        examples=list(row.examples or []),
        source=row.source or "",
        source_sha256=row.source_sha256,
        embedding_model=row.embedding_model,
        created_at=row.created_at.isoformat() if row.created_at else "",
        runs=row.runs,
        failures=row.failures,
        last_error=row.last_error,
    )


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #


async def get_tool(
    session: AsyncSession,
    name: str,
    version: int | None = None,
    statuses: Sequence[str] = ("active",),
) -> Tool | None:
    stmt = select(Tool).where(Tool.name == name)
    if version is not None:
        stmt = stmt.where(Tool.version == version)
    if statuses:
        stmt = stmt.where(Tool.status.in_(list(statuses)))
    stmt = stmt.order_by(desc(Tool.version)).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_versions(session: AsyncSession, name: str) -> list[Tool]:
    stmt = select(Tool).where(Tool.name == name).order_by(desc(Tool.version))
    return list((await session.execute(stmt)).scalars().all())


async def next_version(session: AsyncSession, name: str) -> int:
    current = (await session.execute(select(func.max(Tool.version)).where(Tool.name == name))).scalar_one_or_none()
    return int(current or 0) + 1


async def insert_tool(
    session: AsyncSession,
    *,
    manifest: ToolManifest,
    tier: str,
    executor: str,
    embedding: list[float] | None,
    embedding_model: str,
    created_by: str = "system",
    status: str = "active",
) -> Tool:
    version = await next_version(session, manifest.name)
    row = Tool(
        name=manifest.name,
        version=version,
        status=status,
        tier=tier,
        executor=executor,
        description=manifest.description,
        when_to_use=manifest.when_to_use,
        tags=list(manifest.tags),
        search_text=build_search_text(manifest.name, manifest.description, manifest.when_to_use, manifest.tags),
        params_schema=manifest.params_schema,
        permissions=list(manifest.permissions),
        timeout_s=manifest.timeout_s,
        entrypoint=manifest.entrypoint,
        examples=[e.model_dump() for e in manifest.examples],
        source=manifest.source,
        source_sha256=manifest.source_sha256,
        embedding=embedding,
        embedding_model=embedding_model,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    log.info("registered tool %s v%s (%s/%s)", row.name, row.version, tier, executor)
    return row


async def retire_other_versions(session: AsyncSession, name: str, keep_version: int) -> int:
    """Mark every other active version of ``name`` as retired (single active version)."""
    result = await session.execute(
        update(Tool)
        .where(Tool.name == name, Tool.version != keep_version, Tool.status == "active")
        .values(status="retired", updated_at=func.now())
    )
    return int(result.rowcount or 0)


async def set_status(session: AsyncSession, name: str, version: int, status: str) -> bool:
    result = await session.execute(
        update(Tool).where(Tool.name == name, Tool.version == version).values(status=status, updated_at=func.now())
    )
    return bool(result.rowcount)


# --------------------------------------------------------------------------- #
# removal: retire (hide) and hard delete (really drop the row)
# --------------------------------------------------------------------------- #


async def tool_versions(session: AsyncSession, name: str) -> list[tuple[int, str]]:
    """Every stored ``(version, status)`` of ``name``, newest first.

    Callers use this to report what exists ("version 3 does not exist; there are
    versions 1 (retired) and 2 (active)") instead of a bare "not found".
    """
    stmt = select(Tool.version, Tool.status).where(Tool.name == name).order_by(desc(Tool.version))
    return [(int(row[0]), str(row[1])) for row in (await session.execute(stmt)).all()]


async def versions_of(session: AsyncSession, name: str) -> list[Tool]:
    """Every row of ``name`` (any status or tier), oldest version first."""
    stmt = select(Tool).where(Tool.name == name).order_by(Tool.version)
    return list((await session.execute(stmt)).scalars().all())


async def retire_tool(session: AsyncSession, name: str, version: int | None = None) -> int:
    """Hide a tool by marking it ``retired``; returns how many rows changed.

    ``version`` retires that one version, ``None`` retires every version of the
    name (the whole tool disappears from ``search_tools`` / ``get_tool_schema``,
    because both only look at ``active``, or ``active``+``quarantined``, rows).
    Rows that are already retired are not counted, so the return value is
    "how many tools this call actually hid".
    """
    stmt = update(Tool).where(Tool.name == name, Tool.status != "retired")
    if version is not None:
        stmt = stmt.where(Tool.version == version)
    result = await session.execute(stmt.values(status="retired", updated_at=func.now()))
    changed = int(result.rowcount or 0)
    if changed:
        log.info("retired tool %s%s", name, f" v{version}" if version is not None else " (all versions)")
    return changed


async def _ensure_tombstone_table(session: AsyncSession) -> None:
    """Create ``tool_tombstones`` if this database predates it.

    ``init_db`` uses ``Base.metadata.create_all``, which only runs when the operator
    runs ``db init``; the table must exist for a hard delete to be auditable, so the
    delete path creates it on demand (``checkfirst`` makes this a cheap no-op).
    """

    def _create(connection: Any) -> None:
        Base.metadata.tables["tool_tombstones"].create(connection, checkfirst=True)

    await session.run_sync(lambda _sync_session: _create(_sync_session.connection()))


async def delete_tool(
    session: AsyncSession,
    name: str,
    version: int | None = None,
    hard: bool = False,
    *,
    deleted_by: str = "operator",
    reason: str = "",
) -> int:
    """Remove a tool; returns how many ``tools`` rows the call affected.

    ``hard=False`` (the default) only retires the rows -- see :func:`retire_tool` --
    so nothing is destroyed and a mistaken call is recoverable by flipping the status
    back.

    ``hard=True`` really deletes the ``tools`` rows, and the **run ledger is kept**:
    executions are audit history, and a tool that misbehaved is exactly when the
    operator wants to see what it did.  Concretely:

    * one :class:`~agent.registry.tables.ToolTombstone` row per deleted version
      records name/version/tier/created_by/run counters and who deleted it, so the
      fact of the deletion outlives the row;
    * the ``tool_runs`` rows are kept.  Their ``tool_id`` is set to NULL *before* the
      ``tools`` delete (and stamped into ``args_redacted["tool_ref"]``), which works
      on every schema: a fresh database has ``ON DELETE SET NULL``, and a database
      created before that change still says CASCADE but has nothing left to cascade
      once the id is already NULL.  ``recent_runs`` joins on ``Tool``, so orphaned
      rows are no longer listed per-tool -- they remain in the database (and in the
      tombstone counters) rather than being erased.

    The rows are deleted with :func:`sqlalchemy.delete` (not ORM cascade), so no
    ``Tool`` object has to be loaded.
    """
    if not hard:
        return await retire_tool(session, name, version)

    stmt = select(Tool).where(Tool.name == name)
    if version is not None:
        stmt = stmt.where(Tool.version == version)
    rows = list((await session.execute(stmt)).scalars().all())
    if not rows:
        return 0

    await _ensure_tombstone_table(session)
    ids = [row.id for row in rows]
    now = datetime.now(UTC)

    # unlink the ledger first: keeps the runs, and satisfies the old CASCADE FK too
    await session.execute(
        update(ToolRun)
        .where(ToolRun.tool_id.in_(ids))
        .values(
            tool_id=None,
            args_redacted=ToolRun.args_redacted + cast({LEDGER_REF_KEY: name}, JSONB),
        )
    )
    await session.execute(
        insert(ToolTombstone).values(
            [
                {
                    "name": row.name,
                    "version": row.version,
                    "ref": f"{row.name}@{row.version}",
                    "tier": row.tier,
                    "created_by": row.created_by,
                    "status_at_delete": row.status,
                    "runs": row.runs,
                    "failures": row.failures,
                    "deleted_by": deleted_by,
                    "reason": reason[:500],
                    "deleted_at": now,
                }
                for row in rows
            ]
        )
    )
    result = await session.execute(delete(Tool).where(Tool.id.in_(ids)))
    deleted = int(result.rowcount or 0)
    log.warning(
        "hard-deleted tool %s%s: %d version(s) removed by %s, run ledger kept",
        name,
        f" v{version}" if version is not None else " (all versions)",
        deleted,
        deleted_by,
    )
    return deleted


async def list_tools(
    session: AsyncSession,
    *,
    tier: str | None = None,
    status: str | None = None,
    name_like: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[Tool]:
    stmt = select(Tool)
    if tier:
        stmt = stmt.where(Tool.tier == tier)
    if status:
        stmt = stmt.where(Tool.status == status)
    if name_like:
        stmt = stmt.where(Tool.name.ilike(f"%{name_like}%"))
    stmt = stmt.order_by(Tool.name, desc(Tool.version)).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def count_tools(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(select(Tool.tier, Tool.status, func.count()).group_by(Tool.tier, Tool.status))
    ).all()
    out: dict[str, int] = {}
    for tier, status, count in rows:
        out[f"{tier}/{status}"] = int(count)
    out["total"] = sum(out.values()) if out else 0
    return out


# --------------------------------------------------------------------------- #
# run statistics + automatic quarantine
# --------------------------------------------------------------------------- #


async def record_run(
    session: AsyncSession,
    *,
    tool: Tool,
    session_id: str | None,
    ok: bool,
    duration_ms: int,
    error: str | None = None,
    error_code: str | None = None,
    args: dict[str, Any] | None = None,
    quarantine_after: int = QUARANTINE_AFTER,
) -> str:
    """Insert a run row, update counters, and quarantine repeatedly failing tools.

    Returns the (possibly new) tool status.
    """
    session.add(
        ToolRun(
            tool_id=tool.id,
            session_id=session_id,
            ok=ok,
            duration_ms=int(duration_ms),
            error_code=error_code,
            error=(error or "")[:2000] or None,
            args_redacted=_redact(args or {}),
        )
    )
    status = tool.status
    if ok:
        tool.runs += 1
        tool.consecutive_failures = 0
        tool.status = "active" if tool.status == "active" else tool.status
    else:
        tool.runs += 1
        tool.failures += 1
        tool.consecutive_failures += 1
        tool.last_error = (error or "")[:2000]
        if tool.tier == "generated" and tool.consecutive_failures >= quarantine_after and tool.status == "active":
            tool.status = "quarantined"
            status = "quarantined"
            log.warning("tool %s v%s quarantined after %d consecutive failures", tool.name, tool.version, tool.consecutive_failures)
    tool.updated_at = datetime.now(UTC)
    await session.flush()
    return status


def _redact(args: dict[str, Any], limit: int = 2000) -> dict[str, Any]:
    """Keep argument shapes for debugging without storing huge payloads."""
    out: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > 256:
            out[key] = value[:256] + f"...[{len(value)} chars]"
        elif isinstance(value, str):
            # short strings (paths, names, flags) are the useful part of a run log
            out[key] = value
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, (list, tuple)):
            out[key] = f"<{type(value).__name__} len={len(value)}>"
        elif isinstance(value, dict):
            out[key] = f"<dict keys={sorted(value)[:8]}>"
        else:
            out[key] = f"<{type(value).__name__}>"
    return out


async def recent_runs(session: AsyncSession, name: str, limit: int = 10) -> list[ToolRun]:
    stmt = (
        select(ToolRun)
        .join(Tool, Tool.id == ToolRun.tool_id)
        .where(Tool.name == name)
        .order_by(desc(ToolRun.id))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------- #
# chat sessions / messages
# --------------------------------------------------------------------------- #


async def ensure_session(session: AsyncSession, session_id: str, title: str = "") -> Session:
    row = await session.get(Session, session_id)
    now = datetime.now(UTC)
    if row is None:
        row = Session(id=session_id, title=title, meta={}, created_at=now, last_seen_at=now)
        session.add(row)
        await session.flush()
    else:
        row.last_seen_at = now
        if title and not row.title:
            row.title = title
    return row


async def append_message(
    session: AsyncSession,
    session_id: str,
    role: str,
    content: dict[str, Any],
    tokens: int = 0,
) -> None:
    session.add(Message(session_id=session_id, role=role, content=content, tokens=tokens))


async def load_messages(session: AsyncSession, session_id: str, limit: int = 40) -> list[Message]:
    stmt = select(Message).where(Message.session_id == session_id).order_by(desc(Message.id)).limit(limit)
    rows = list((await session.execute(stmt)).scalars().all())
    rows.reverse()
    return rows


async def clear_messages(session: AsyncSession, session_id: str) -> int:
    result = await session.execute(delete(Message).where(Message.session_id == session_id))
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# sandbox bindings (bookkeeping only -- the control plane owns the VMs)
# --------------------------------------------------------------------------- #


async def bind_sandbox(session: AsyncSession, session_id: str, vm_id: str, ttl_s: float) -> SandboxSessionRecord:
    row = await session.get(SandboxSessionRecord, session_id)
    expires = datetime.now(UTC) + timedelta(seconds=ttl_s)
    if row is None:
        row = SandboxSessionRecord(session_id=session_id, vm_id=vm_id, state="ready", expires_at=expires)
        session.add(row)
    else:
        row.vm_id = vm_id
        row.state = "ready"
        row.expires_at = expires
    await session.flush()
    return row


async def unbind_sandbox(session: AsyncSession, session_id: str) -> None:
    row = await session.get(SandboxSessionRecord, session_id)
    if row is not None:
        row.state = "released"
        await session.flush()


async def tool_summaries(session: AsyncSession, names: Sequence[str]) -> list[ToolRecord]:
    if not names:
        return []
    stmt = select(Tool).where(Tool.name.in_(list(names)), Tool.status == "active")
    rows = list((await session.execute(stmt)).scalars().all())
    best: dict[str, Tool] = {}
    for row in rows:
        if row.name not in best or row.version > best[row.name].version:
            best[row.name] = row
    return [record_from_row(best[name]) for name in names if name in best]


def summary_line(row: Tool) -> str:
    return f"{row.name} v{row.version}: {summarize_schema(row.params_schema)}"


async def tools_missing_embedding(session: AsyncSession, model: str) -> list[tuple[str, int]]:
    """(name, version) of tools whose embedding came from a different model."""
    stmt = select(Tool.name, Tool.version).where(
        Tool.status == "active",
        or_(Tool.embedding_model.is_(None), Tool.embedding_model != model),
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


async def set_embedding(session: AsyncSession, name: str, version: int, embedding: list[float], model: str) -> None:
    await session.execute(
        update(Tool)
        .where(Tool.name == name, Tool.version == version)
        .values(embedding=embedding, embedding_model=model)
    )
