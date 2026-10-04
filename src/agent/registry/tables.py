"""SQLAlchemy tables for the tool registry.

Only PostgreSQL is supported: the tool embedding column is ``vector(1024)`` from
pgvector and retrieval uses ``pg_trgm`` for the keyword half of the hybrid search.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 1024


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


class Tool(Base):
    """One immutable version of a tool."""

    __tablename__ = "tools"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    tier: Mapped[str] = mapped_column(String(16), nullable=False, default="generated")
    executor: Mapped[str] = mapped_column(String(24), nullable=False, default="sandbox_python")

    description: Mapped[str] = mapped_column(Text, nullable=False)
    when_to_use: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    params_schema: Mapped[dict] = mapped_column(JSONB, nullable=False)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    timeout_s: Mapped[float] = mapped_column(Float, nullable=False, default=30.0)
    entrypoint: Mapped[str] = mapped_column(String(64), nullable=False, default="run")
    examples: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    source: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    runs_rel: Mapped[list[ToolRun]] = relationship(back_populates="tool", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ux_tools_name_version", "name", "version", unique=True),
        Index("ix_tools_name_status", "name", "status"),
        Index("ix_tools_tier_status", "tier", "status"),
        Index("ix_tools_tags", "tags", postgresql_using="gin"),
        Index(
            "ix_tools_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
        Index(
            "ix_tools_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class ToolRun(Base):
    """One execution of a tool, used for statistics and automatic quarantine."""

    __tablename__ = "tool_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Nullable + SET NULL: a *hard* tool delete removes the tool row but must not erase
    # the execution ledger (audit history).  ``repository.delete_tool`` additionally
    # nulls this column itself before deleting, so the ledger survives even on a
    # database whose constraint still says CASCADE (see that docstring).
    tool_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tools.id", ondelete="SET NULL"), nullable=True
    )
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    args_redacted: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    tool: Mapped[Tool] = relationship(back_populates="runs_rel")

    __table_args__ = (Index("ix_tool_runs_tool_created", "tool_id", "created_at"),)


class ToolTombstone(Base):
    """Audit record of a tool that was hard-deleted from the registry.

    A hard delete really removes the ``tools`` rows (the registry has no soft-delete
    flag for the operator path), so without this table the *fact* that a tool once
    existed -- and who removed it -- would be gone.  ``tool_runs`` rows are kept and
    their ``tool_id`` is set to NULL, which keeps their execution history but loses
    the name; ``ref`` ("name@version") is what ties them back together.
    """

    __tablename__ = "tool_tombstones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    ref: Mapped[str] = mapped_column(String(80), nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status_at_delete: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deleted_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_tool_tombstones_name", "name"),)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_messages_session_id", "session_id", "id"),)


class SandboxSessionRecord(Base):
    __tablename__ = "sandbox_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    vm_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
