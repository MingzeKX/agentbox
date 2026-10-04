"""Removing tools: retire (hide) and hard delete, for the agent and for the operator.

The repository tests run against an in-memory fake session rather than PostgreSQL:
the suite must pass without a database, and the three new repository functions are
plain ``UPDATE``/``DELETE`` statements.  The fake evaluates the *real* SQLAlchemy
statement (its compiled WHERE clause) against a list of real ``Tool`` objects, so a
wrong column, a wrong operator or a wrong ``rowcount`` still fails the test.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Column, cast, delete, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import operators
from sqlalchemy.sql.dml import Delete, Insert, Update
from sqlalchemy.sql.elements import BinaryExpression, BindParameter, BooleanClauseList

from agent.ai import app as app_module
from agent.config import settings
from agent.models.tool import ToolManifest
from agent.registry import repository as repo
from agent.registry.tables import Tool, ToolRun, ToolTombstone
from agent.toolsmith import gates
from agent.toolsmith.gates import ToolsmithContext, retire

# --------------------------------------------------------------------------- #
# a session that really evaluates the statements the repository builds
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    return datetime.now(UTC)


def make_tool(
    name: str = "upper_text",
    version: int = 1,
    *,
    status: str = "active",
    tier: str = "generated",
    created_by: str = "model",
) -> Tool:
    return Tool(
        id=f"{name}-{version}",
        name=name,
        version=version,
        status=status,
        tier=tier,
        executor="sandbox_python" if tier == "generated" else "host_native",
        description=f"{name} v{version}",
        when_to_use="test fixture",
        tags=["test"],
        search_text=name,
        params_schema={"type": "object", "properties": {}, "additionalProperties": False},
        permissions=[],
        timeout_s=30.0,
        entrypoint="run",
        examples=[],
        source="def run(args):\n    return {}\n",
        source_sha256=f"sha-{name}-{version}",
        embedding_model="fake",
        created_by=created_by,
        created_at=_now(),
        updated_at=_now(),
        runs=version,
        failures=0,
    )


class FakeResult:
    def __init__(self, rows: list[Any] | None = None, rowcount: int = 0) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalars(self) -> FakeResult:
        return self

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _Predicate:
    """Evaluates the *real* WHERE clause of a statement against the row it selects.

    It walks the SQLAlchemy expression tree instead of the rendered SQL, so a wrong
    column, the wrong operator or a forgotten ``status`` filter in ``repository.py``
    still makes the test fail -- which is the point of testing against a fake session.

    ``rows`` is keyed by table name because ``recent_runs`` selects ``tool_runs`` joined
    to ``tools``: an INNER JOIN whose ``tools`` half is missing (the tool was deleted, so
    its runs keep a NULL ``tool_id``) contributes no row -- exactly like PostgreSQL, and
    exactly what keeps an orphaned run out of the per-tool history.
    """

    def __init__(self, rows: dict[str, Any], params: dict[str, Any]) -> None:
        self.rows = rows
        self.params = params

    def resolve(self, node: Any) -> Any:
        if isinstance(node, Column):
            table = node.table.name
            if table not in self.rows:
                raise AssertionError(
                    f"the statement filters on {table}.{node.key}, which this query does not select"
                )
            row = self.rows[table]
            # a missing INNER JOIN partner makes every predicate on that table false
            return None if row is None else getattr(row, node.key)
        if isinstance(node, BindParameter):
            return node.value
        if isinstance(node, BooleanClauseList):
            return self.matches(node)
        return node

    def matches(self, clause: Any) -> bool:
        if clause is None:
            return True
        if isinstance(clause, BooleanClauseList):
            outcomes = [self.matches(child) for child in clause.clauses]
            return all(outcomes) if clause.operator is operators.and_ else any(outcomes)
        if isinstance(clause, BinaryExpression):
            left, right = self.resolve(clause.left), self.resolve(clause.right)
            if clause.operator is operators.in_op:
                return left in list(right)
            if clause.operator is operators.notin_op:
                return left not in list(right)
            if clause.operator is operators.is_:
                return left is right
            if clause.operator is operators.is_not:
                return left is not right
            if left is None or right is None:
                return False
            return bool(clause.operator(left, right))
        return True


def _matches(stmt: Any, **rows: Any) -> bool:
    """``rows`` is keyed by table name: ``_matches(stmt, tools=row)``."""
    where = getattr(stmt, "whereclause", None)
    if where is None:
        return True
    return _Predicate(rows, stmt.compile().params).matches(where)


class FakeRegistrySession:
    """Enough of an AsyncSession for the registry repository helpers."""

    def __init__(self) -> None:
        self.tools: list[Tool] = []
        self.ledger: list[ToolRun] = []
        self.tombstones: list[dict[str, Any]] = []
        self.ledger_unlinks: list[dict[str, Any]] = []
        self.run_sync_calls = 0
        self.commits = 0

    # --------------------------------------------------------------- fixtures
    def seed(self, *rows: Tool, with_runs: bool = True) -> None:
        self.tools.extend(rows)
        if with_runs:
            for row in rows:
                for index in range(row.runs):
                    self.ledger.append(
                        ToolRun(
                            id=len(self.ledger) + 1,
                            tool_id=row.id,
                            session_id="s-test",
                            ok=True,
                            duration_ms=index,
                            args_redacted={"path": "/workspace/a.txt"},
                            created_at=_now(),
                        )
                    )

    def statuses(self, name: str) -> list[tuple[int, str]]:
        return sorted((row.version, row.status) for row in self.tools if row.name == name)

    # -------------------------------------------------------------- db surface
    async def execute(self, stmt: Any) -> FakeResult:
        if isinstance(stmt, Insert):
            # ``values()`` stores a dict per row, ``values([...])`` a tuple of dicts
            values = list(stmt._values or stmt._multi_values[0])  # noqa: SLF001 - how the ORM stores values()
            for entry in values:
                self.tombstones.append({_key(key): _value(value) for key, value in entry.items()})
            return FakeResult(rowcount=len(values))
        if isinstance(stmt, Update):
            return self._update(stmt)
        if isinstance(stmt, Delete):
            return self._delete(stmt)
        return self._select(stmt)

    def _update(self, stmt: Any) -> FakeResult:
        assignments = {_key(key): _value(value) for key, value in stmt._values.items()}  # noqa: SLF001
        if _target_class(stmt) is ToolRun:
            matched = [row for row in self.ledger if _matches(stmt, tool_runs=row)]
            for row in matched:
                self.ledger_unlinks.append(
                    {
                        "expression": str(assignments["args_redacted"]),
                        "sentinel": _sentinel(assignments["args_redacted"]),
                        "tool_id": assignments["tool_id"],
                        "run_id": row.id,
                    }
                )
                row.tool_id = None
            return FakeResult(rowcount=len(matched))
        rows = [row for row in self.tools if _matches(stmt, tools=row)]
        for row in rows:
            for key, value in assignments.items():
                if key == "updated_at":
                    setattr(row, key, _now())
                elif key not in {"tool_id", "args_redacted"}:
                    setattr(row, key, value)
        return FakeResult(rowcount=len(rows))

    def _delete(self, stmt: Any) -> FakeResult:
        targets = _target_class(stmt)
        if targets is Tool:
            removed = [row for row in self.tools if _matches(stmt, tools=row)]
            self.tools = [row for row in self.tools if row not in removed]
            # the fake models the *fresh* schema (ON DELETE SET NULL): the run rows
            # survive, and only an FK that still says CASCADE would drop them -- which
            # is exactly why the repository nulls tool_id itself first.
            return FakeResult(rowcount=len(removed))
        if targets is ToolRun:
            removed_runs = [row for row in self.ledger if _matches(stmt, tool_runs=row)]
            self.ledger = [row for row in self.ledger if row not in removed_runs]
            return FakeResult(rowcount=len(removed_runs))
        if targets is ToolTombstone:
            return FakeResult(rowcount=len(self.tombstones))
        return FakeResult()

    def _select(self, stmt: Any) -> FakeResult:
        targets = _target_class(stmt)
        # only real columns: limit/offset/order_by elements are not columns at all
        columns = [getattr(column, "key", None) for column in stmt.exported_columns]
        keys = [key for key in columns if key in Tool.__table__.c]
        rows = [row for row in self.tools if _matches(stmt, tools=row)] if targets is Tool else []
        runs = (
            [row for row in self.ledger if _matches(stmt, tool_runs=row, tools=self._joined_tool(row))]
            if targets is ToolRun
            else []
        )
        if targets is Tool and keys == ["version", "status"]:
            rows.sort(key=lambda row: -row.version)
            return FakeResult([(row.version, row.status) for row in rows])
        if targets is Tool and keys == ["id"]:
            return FakeResult([(row.id,) for row in rows])
        if targets is Tool:
            rows.sort(key=lambda row: row.version)
            return FakeResult(rows)
        if targets is ToolRun:
            return FakeResult(runs)
        return FakeResult()

    def _joined_tool(self, run: Any) -> Any:
        """The ``tools`` row an INNER JOIN pairs with this run (``None`` for an orphan)."""
        if not isinstance(run, ToolRun) or run.tool_id is None:
            return None
        return next((tool for tool in self.tools if tool.id == run.tool_id), None)

    async def run_sync(self, fn: Any) -> None:
        # the tombstone table "already exists" in the fake database
        self.run_sync_calls += 1

    def add(self, obj: Any) -> None:
        if isinstance(obj, ToolRun):
            self.ledger.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def __aenter__(self) -> FakeRegistrySession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _target_class(stmt: Any) -> Any:
    # ``select(ToolRun).join(Tool, ...)`` knows its entity; its FROM element is a join
    # object with no table name, so ask the statement what it actually selects first.
    descriptions = getattr(stmt, "column_descriptions", None) or []
    entity = descriptions[0].get("entity") if descriptions else None
    if entity in (Tool, ToolRun, ToolTombstone):
        return entity
    table = getattr(stmt, "table", None)
    if table is None:
        froms = getattr(stmt, "get_final_froms", lambda: [])()
        table = froms[0] if froms else None
    if table is None:
        return None
    return {"tools": Tool, "tool_runs": ToolRun, "tool_tombstones": ToolTombstone}.get(table.name)


def _key(key: Any) -> str:
    """``values()`` keys are strings in some SQLAlchemy versions, Columns in others."""
    return getattr(key, "key", key)


def _value(value: Any) -> Any:
    """``values(status="retired")`` stores the literal, but also accepts expressions."""
    return value.value if isinstance(value, BindParameter) else value


def _children(node: Any) -> list[Any]:
    getter = getattr(node, "get_children", None)
    return list(getter()) if callable(getter) else []


def _sentinel(expression: Any) -> Any:
    """The dict a ``args_redacted || cast({...} AS JSONB)`` expression writes.

    The repository wraps the literal in ``cast(..., JSONB)``, so the dict is *not*
    ``expression.right`` itself -- it is a node underneath it.  Walk the tree instead of
    assuming a shape, so re-parenting the expression fails here only if the value the
    database would really store changed.
    """
    stack = [expression]
    while stack:
        node = stack.pop()
        if isinstance(node, BindParameter) and isinstance(node.value, dict):
            return node.value
        stack.extend(_children(node))
    return None


class FakeSessionmaker:
    """Callable async-session factory that records how often a session was opened."""

    def __init__(self, session: FakeRegistrySession | None = None) -> None:
        self.session = session or FakeRegistrySession()
        self.opened = 0

    def __call__(self) -> FakeRegistrySession:
        self.opened += 1
        return self.session


# --------------------------------------------------------------------------- #
# repository: retire
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_retire_one_version_leaves_the_others_alone():
    session = FakeRegistrySession()
    session.seed(make_tool(version=1), make_tool(version=2), make_tool(version=3))

    assert await repo.retire_tool(session, "upper_text", 2) == 1
    assert session.statuses("upper_text") == [(1, "active"), (2, "retired"), (3, "active")]

    # already retired -> nothing changed, so a retry is not reported as a removal
    assert await repo.retire_tool(session, "upper_text", 2) == 0


@pytest.mark.asyncio
async def test_retire_without_a_version_hides_every_version():
    session = FakeRegistrySession()
    session.seed(make_tool(version=1), make_tool(version=2), make_tool(version=3, status="quarantined"))

    assert await repo.retire_tool(session, "upper_text") == 3
    assert session.statuses("upper_text") == [(1, "retired"), (2, "retired"), (3, "retired")]
    assert await repo.retire_tool(session, "upper_text") == 0


@pytest.mark.asyncio
async def test_retired_versions_never_reach_search_tools():
    """The search filter is the whole point of retiring: retired shares the row."""
    session = FakeRegistrySession()
    session.seed(make_tool(version=1), make_tool(version=2))

    await repo.retire_tool(session, "upper_text", 1)

    active = await repo.list_tools(session, status="active")
    assert [(row.version, row.status) for row in active] == [(2, "active")]
    assert await repo.get_tool(session, "upper_text", 1) is None, "get_tool only looks at active rows"
    # the row itself is still there, which is what makes retiring reversible
    assert await repo.tool_versions(session, "upper_text") == [(2, "active"), (1, "retired")]


# --------------------------------------------------------------------------- #
# repository: hard delete
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_hard_delete_removes_the_row_and_keeps_the_run_ledger():
    session = FakeRegistrySession()
    session.seed(make_tool(version=1), make_tool(version=2))
    session.seed(make_tool(name="other_tool", version=1), with_runs=False)

    assert await repo.delete_tool(session, "upper_text", 1, hard=True) == 1

    assert await repo.tool_versions(session, "upper_text") == [(2, "active")]
    assert [row.name for row in session.tools] == ["upper_text", "other_tool"], "only v1 is gone"

    # audit history: the tombstone records what was deleted, by whom
    assert len(session.tombstones) == 1
    stone = session.tombstones[0]
    assert stone["ref"] == "upper_text@1"
    assert (stone["name"], stone["version"], stone["tier"], stone["created_by"]) == (
        "upper_text",
        1,
        "generated",
        "model",
    )
    assert stone["deleted_by"] == "operator"
    assert stone["runs"] == 1

    # the ledger survives, unlinked from the deleted row and stamped with its identity
    assert len(session.ledger) == 3, "the v1 run is kept, not cascaded away"
    unlinked = [row for row in session.ledger if row.tool_id is None]
    assert [row.id for row in unlinked] == [1], "only the deleted version's run lost its tool_id"
    assert [row.tool_id for row in session.ledger if row.tool_id] == ["upper_text-2", "upper_text-2"]
    assert len(session.ledger_unlinks) == 1
    assert session.ledger_unlinks[0]["sentinel"] == {"tool_ref": "upper_text"}
    assert "||" in session.ledger_unlinks[0]["expression"], "JSONB concatenation, not an overwrite"
    # v2 still exists, so its runs stay visible per-tool; only the deleted version's
    # orphaned run drops out (recent_runs INNER JOINs tools on tool_id)
    assert sorted(row.id for row in await repo.recent_runs(session, "upper_text")) == [2, 3]
    assert session.run_sync_calls == 2, "tombstone table + ledger-unlink repair, both before use"


@pytest.mark.asyncio
async def test_hard_delete_without_a_version_purges_every_version():
    session = FakeRegistrySession()
    session.seed(make_tool(version=1), make_tool(version=2, status="retired"))

    assert await repo.delete_tool(session, "upper_text", hard=True, deleted_by="operator", reason="unused") == 2

    assert session.tools == []
    assert await repo.tool_versions(session, "upper_text") == []
    assert sorted(stone["ref"] for stone in session.tombstones) == ["upper_text@1", "upper_text@2"]
    assert all(stone["reason"] == "unused" for stone in session.tombstones)
    assert len(session.ledger) == 3, "every run row is kept"
    assert sum(stone["runs"] for stone in session.tombstones) == 3, "the counters live on in the tombstone"


@pytest.mark.asyncio
async def test_delete_without_hard_only_retires():
    session = FakeRegistrySession()
    session.seed(make_tool(version=1))

    assert await repo.delete_tool(session, "upper_text", 1) == 1

    assert session.statuses("upper_text") == [(1, "retired")]
    assert session.tombstones == [], "a soft delete must not record a tombstone"
    assert session.ledger[0].tool_id == "upper_text-1", "nothing was unlinked"
    assert session.run_sync_calls == 0


@pytest.mark.asyncio
async def test_delete_of_an_unknown_tool_is_a_no_op():
    session = FakeRegistrySession()

    assert await repo.delete_tool(session, "nope", hard=True) == 0
    assert session.tombstones == []
    assert session.run_sync_calls == 0, "nothing to delete -> no tombstone table work"


def test_ledger_columns_stay_a_kept_history():
    """The schema documents the choice: a deleted tool must not take its runs with it."""
    tool_run = ToolRun.__table__
    assert tool_run.c.tool_id.nullable is True
    assert {fk.ondelete for fk in tool_run.c.tool_id.foreign_keys} == {"SET NULL"}
    assert "tool_tombstones" in ToolTombstone.metadata.tables


class FakeSyncSession:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def connection(self) -> Any:
        return self._connection


class FakeDdlSession:
    """Enough of an AsyncSession for a ``run_sync`` DDL guard."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def run_sync(self, fn: Any) -> None:
        fn(FakeSyncSession(self._connection))


class FakeScalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar(self) -> Any:
        return self._value


class FakeConnection:
    """Records the DDL a guard issues and answers the foreign-key definition query."""

    def __init__(self, definition: str) -> None:
        self.statements: list[str] = []
        self.definition = definition

    def exec_driver_sql(self, statement: str, parameters: Any = None) -> Any:  # noqa: ARG002
        collapsed = " ".join(statement.split())
        self.statements.append(collapsed)
        if collapsed.lower().startswith("select"):
            return FakeScalar(self.definition)
        return None


def test_a_hard_delete_repairs_a_ledger_that_cannot_be_unlinked():
    """A database from before the ledger became history has tool_id NOT NULL.

    Live failure (real traceback from the platform VM)::

        IntegrityError: null value in column "tool_id" of relation "tool_runs"
        violates not-null constraint
        [SQL: UPDATE tool_runs SET tool_id=$1::VARCHAR, ...]

    The admin API answered HTTP 500 with an empty body, so the operator's
    ``/tools delete <name> --purge`` looked like it did nothing while retire worked.
    ``create_all`` never alters an existing table, so the delete path has to.
    """
    connection = FakeConnection("FOREIGN KEY (tool_id) REFERENCES tools(id) ON DELETE CASCADE")

    asyncio.run(repo._ensure_ledger_unlinkable(FakeDdlSession(connection)))  # type: ignore[arg-type]

    joined = "\n".join(connection.statements)
    assert "ALTER TABLE tool_runs ALTER COLUMN tool_id DROP NOT NULL" in joined
    assert "DROP CONSTRAINT tool_runs_tool_id_fkey" in joined
    assert "ADD CONSTRAINT tool_runs_tool_id_fkey" in joined
    assert "ON DELETE SET NULL" in joined, "the ledger is kept, only the reference is dropped"


def test_a_hard_delete_leaves_an_already_correct_ledger_alone():
    connection = FakeConnection("FOREIGN KEY (tool_id) REFERENCES tools(id) ON DELETE SET NULL")

    asyncio.run(repo._ensure_ledger_unlinkable(FakeDdlSession(connection)))  # type: ignore[arg-type]

    assert any("DROP NOT NULL" in statement for statement in connection.statements), (
        "DROP NOT NULL is idempotent and always safe"
    )
    assert not any("ADD CONSTRAINT" in statement for statement in connection.statements), (
        "an already SET NULL foreign key is not rewritten"
    )


@pytest.mark.asyncio
async def test_the_delete_path_repairs_the_ledger_before_unlinking():
    """The repair must happen *inside* delete_tool, before the UPDATE that needs it."""
    session = FakeRegistrySession()
    session.seed(make_tool(version=1))
    order: list[str] = []
    original = repo._ensure_ledger_unlinkable

    async def spy(target):
        order.append("guard")
        return await original(target)

    real_execute = session.execute

    async def execute(stmt):
        if isinstance(stmt, Update) and _target_class(stmt) is ToolRun:
            order.append("unlink")
        return await real_execute(stmt)

    session.execute = execute  # type: ignore[method-assign]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(repo, "_ensure_ledger_unlinkable", spy)
        assert await repo.delete_tool(session, "upper_text", 1, hard=True) == 1

    assert order == ["guard", "unlink"], "the column is repaired before it is set to NULL"


def test_tombstone_columns_carry_the_audit_fields():
    """Guard the shape the delete path inserts (a wrong key is a runtime failure)."""
    columns = set(ToolTombstone.__table__.c.keys())
    assert {
        "name",
        "version",
        "ref",
        "tier",
        "created_by",
        "status_at_delete",
        "runs",
        "failures",
        "deleted_by",
        "reason",
        "deleted_at",
    } <= columns
    stone = ToolTombstone(ref="upper_text@3", name="upper_text", version=3, runs=7, failures=1, deleted_by="operator")
    assert stone.ref == "upper_text@3"
    assert json.dumps({"ref": stone.ref, "runs": stone.runs})


def test_ledger_sentinel_uses_jsonb_concat():
    """``args_redacted || {"tool_ref": name}`` must stay a JSONB concatenation."""
    expression = ToolRun.args_redacted + cast({"tool_ref": "upper_text"}, JSONB)
    assert "||" in str(expression)
    assert str(expression.type) == "JSONB", "the concatenation stays JSONB, not text"
    assert _sentinel(expression) == {"tool_ref": "upper_text"}, "the ref travels inside the cast"


# --------------------------------------------------------------------------- #
# the agent-facing handler
# --------------------------------------------------------------------------- #


def gate_context(sessionmaker: FakeSessionmaker) -> ToolsmithContext:
    return ToolsmithContext(session_id="s-retire", gateway=None, sessionmaker=sessionmaker)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_agent_retires_its_own_tool():
    session = FakeRegistrySession()
    session.seed(make_tool(version=1, status="retired"), make_tool(version=2))
    maker = FakeSessionmaker(session)

    result = await retire(gate_context(maker), {"name": "upper_text", "version": 2})

    assert result["ok"] is True
    assert result["name"] == "upper_text"
    assert result["versions_retired"] == [2]
    assert result["purged"] is False
    assert result["remaining"] == []
    assert session.statuses("upper_text") == [(1, "retired"), (2, "retired")]
    assert session.commits == 1


@pytest.mark.asyncio
async def test_agent_purges_its_own_tool_only_with_purge():
    session = FakeRegistrySession()
    session.seed(make_tool(version=1))
    maker = FakeSessionmaker(session)

    result = await retire(gate_context(maker), {"name": "upper_text", "purge": True})

    assert result["ok"] is True and result["purged"] is True
    assert result["versions_retired"] == [1]
    assert session.tools == [], "purge really deletes the row"
    assert len(session.tombstones) == 1
    assert session.tombstones[0]["deleted_by"] == "model"


@pytest.mark.asyncio
async def test_agent_cannot_retire_a_seeded_core_tool():
    session = FakeRegistrySession()
    session.seed(make_tool(name="csv_summary", version=1, tier="core", created_by="seed"))
    maker = FakeSessionmaker(session)

    result = await retire(gate_context(maker), {"name": "csv_summary"})

    assert result["ok"] is False
    assert result["purged"] is False
    assert result["versions_retired"] == []
    assert "not a tool this agent wrote" in result["error"]
    assert "core" in result["error"] and "operator" in result["error"]
    assert session.statuses("csv_summary") == [(1, "active")], "the refusal changed nothing"
    assert "POST /admin/tools/retire" in result["error"], "the message points at the operator path"


@pytest.mark.asyncio
async def test_agent_cannot_purge_a_core_tool_either():
    session = FakeRegistrySession()
    session.seed(make_tool(name="csv_summary", version=1, tier="core", created_by="seed"))
    maker = FakeSessionmaker(session)

    result = await retire(gate_context(maker), {"name": "csv_summary", "purge": True})

    assert result["ok"] is False
    assert session.tools, "the row is still there"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    ["toolsmith.create", "fs.read", "exec.run", "sandbox.reset", "net.fetch", "mcp.sync", "time.now", "host.exec"],
)
async def test_agent_cannot_retire_the_built_in_surface(name):
    maker = FakeSessionmaker()

    result = await retire(gate_context(maker), {"name": name})

    assert result["ok"] is False
    assert "built-in tool" in result["error"]
    assert maker.opened == 0, "a protected name is refused before the registry is even queried"


@pytest.mark.asyncio
async def test_unknown_tool_name_is_a_helpful_error():
    maker = FakeSessionmaker(FakeRegistrySession())

    result = await retire(gate_context(maker), {"name": "ghost_tool"})

    assert result["ok"] is False
    assert "no tool named 'ghost_tool' is registered" in result["error"]
    assert "toolsmith.list_mine" in result["error"]
    assert result["remaining"] == []


@pytest.mark.asyncio
async def test_unknown_version_lists_the_versions_that_exist():
    session = FakeRegistrySession()
    session.seed(make_tool(version=1, status="retired"), make_tool(version=2))
    maker = FakeSessionmaker(session)

    result = await retire(gate_context(maker), {"name": "upper_text", "version": 9})

    assert result["ok"] is False
    assert "no version 9" in result["error"]
    assert "v2 (active)" in result["error"] and "v1 (retired)" in result["error"]
    assert result["remaining"] == [2, 1]
    assert session.statuses("upper_text") == [(1, "retired"), (2, "active")]


@pytest.mark.asyncio
async def test_missing_or_bad_arguments_are_explained():
    maker = FakeSessionmaker()

    no_name = await retire(gate_context(maker), {})
    assert no_name["ok"] is False and "argument 'name' is required" in no_name["error"]

    bad_version = await retire(gate_context(maker), {"name": "upper_text", "version": "two"})
    assert bad_version["ok"] is False and "must be an integer" in bad_version["error"]
    assert maker.opened == 0, "arguments are validated before the registry is touched"


# --------------------------------------------------------------------------- #
# seeding + handler coverage
# --------------------------------------------------------------------------- #


def test_toolsmith_retire_is_seeded_as_a_core_tool():
    from agent.registry.seed import CORE_TOOLS, HOST_NATIVE_TOOLS

    spec = next(item for item in CORE_TOOLS if item["name"] == "toolsmith.retire")
    assert spec["executor"] == "host_native"
    assert "toolsmith.retire" in HOST_NATIVE_TOOLS
    assert 0 < spec["timeout_s"] <= 15
    assert spec["params_schema"]["required"] == ["name"]
    assert set(spec["params_schema"]["properties"]) == {"name", "version", "purge"}
    assert "core" in spec["when_to_use"], "the model must be told core tools cannot be removed"
    assert "toolsmith.retire" in gates.TOOLSMITH_HANDLERS


def test_the_seeded_spec_is_a_valid_manifest():
    from agent.registry.seed import CORE_TOOLS

    spec = next(item for item in CORE_TOOLS if item["name"] == "toolsmith.retire")
    manifest = ToolManifest.model_validate(
        {
            "name": spec["name"],
            "description": spec["description"],
            "when_to_use": spec["when_to_use"],
            "tags": spec["tags"],
            "params_schema": spec["params_schema"],
            "timeout_s": spec["timeout_s"],
            "source": spec["source"],
            "tests": [],
        }
    )
    assert manifest.name == "toolsmith.retire"


def test_toolsmith_handlers_still_cover_every_host_native_core_tool():
    """The gates-coverage rule, repeated here because this change adds a core tool."""
    from agent.ai.builtins import BUILTIN_HANDLERS
    from agent.ai.mcp_tools import MCP_HANDLERS
    from agent.ai.net import NET_HANDLERS
    from agent.ai.pull import PULL_HANDLERS
    from agent.registry.seed import CORE_TOOLS

    host_tools = {spec["name"] for spec in CORE_TOOLS if spec["executor"] == "host_native"}
    assert host_tools == (
        set(gates.TOOLSMITH_HANDLERS)
        | set(NET_HANDLERS)
        | set(MCP_HANDLERS)
        | set(PULL_HANDLERS)
        | set(BUILTIN_HANDLERS)
    )


def test_protected_names_cover_the_documented_surface():
    for name in ("toolsmith.check", "net.fetch", "fs.read", "exec.run", "sandbox.reset", "mcp.sync"):
        assert gates._is_protected(name), name
    assert gates._is_protected("time.now") and gates._is_protected("host.exec")
    assert not gates._is_protected("upper_text")


# --------------------------------------------------------------------------- #
# operator admin API
# --------------------------------------------------------------------------- #


class FakeRequest:
    def __init__(self, body: Any = None, token: str | None = None) -> None:
        self._body = body
        self.headers = {"X-Agent-Token": token} if token else {}

    async def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body


def admin_endpoint(path: str):
    app = app_module.create_app()
    route = next(route for route in app.routes if getattr(route, "path", None) == path)
    return app, route.endpoint


def _decode(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.body
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_admin_inventory_lists_every_row_with_the_operator_fields(monkeypatch):
    session = FakeRegistrySession()
    session.seed(make_tool(name="csv_summary", version=1, tier="core", created_by="seed"))
    session.seed(make_tool(name="upper_text", version=2), with_runs=False)
    monkeypatch.setattr(app_module.registry_db, "get_sessionmaker", lambda: FakeSessionmaker(session))
    monkeypatch.setattr(settings, "control_secret", "test-token")
    app, endpoint = admin_endpoint("/admin/tools")
    route = next(route for route in app.routes if getattr(route, "path", None) == "/admin/tools")
    assert {field.name for field in route.dependant.query_params} == {"limit", "offset"}, (
        "the console paginates the inventory over HTTP; FastAPI resolves these defaults, so "
        "the direct call below has to pass them itself"
    )

    payload = _decode(await endpoint(FakeRequest(token="test-token"), limit=200, offset=0))

    assert payload["ok"] is True and payload["count"] == 2
    rows = {row["name"]: row for row in payload["tools"]}
    assert rows["csv_summary"] == {
        "name": "csv_summary",
        "version": 1,
        "status": "active",
        "tier": "core",
        "created_by": "seed",
        "executor": "host_native",
        "runs": 1,
        "failures": 0,
    }
    assert rows["upper_text"]["created_by"] == "model"


@pytest.mark.asyncio
async def test_admin_routes_reject_an_unauthenticated_caller(monkeypatch):
    monkeypatch.setattr(app_module.registry_db, "get_sessionmaker", lambda: FakeSessionmaker())
    monkeypatch.setattr(settings, "control_secret", "test-token")

    for path in ("/admin/tools", "/admin/tools/retire", "/admin/tools/delete"):
        _, endpoint = admin_endpoint(path)
        if path == "/admin/tools":
            response = await endpoint(FakeRequest())
        else:
            response = await endpoint(FakeRequest({"name": "upper_text"}))
        assert response.status_code == 401, path
        assert json.loads(response.body) == {"ok": False, "error": "unauthorized"}

    # a wrong token is just as unauthorized
    _, endpoint = admin_endpoint("/admin/tools")
    assert (await endpoint(FakeRequest(token="nope"))).status_code == 401


@pytest.mark.asyncio
async def test_admin_retire_requires_a_name_and_reports_what_is_left(monkeypatch):
    session = FakeRegistrySession()
    session.seed(make_tool(version=1), make_tool(version=2))
    monkeypatch.setattr(app_module.registry_db, "get_sessionmaker", lambda: FakeSessionmaker(session))
    monkeypatch.setattr(settings, "control_secret", "test-token")
    _, endpoint = admin_endpoint("/admin/tools/retire")

    bad = await endpoint(FakeRequest({"version": 1}, token="test-token"))
    assert bad.status_code == 400
    assert json.loads(bad.body)["error"].startswith('expected {"name"')

    payload = _decode(await endpoint(FakeRequest({"name": "upper_text", "version": 1}, token="test-token")))
    assert payload == {
        "ok": True,
        "name": "upper_text",
        "version": 1,
        "retired": 1,
        "remaining": [{"version": 2, "status": "active"}, {"version": 1, "status": "retired"}],
    }

    everything = _decode(await endpoint(FakeRequest({"name": "upper_text"}, token="test-token")))
    assert everything["retired"] == 1, "v2 was the only row left active"
    assert everything["remaining"] == [
        {"version": 2, "status": "retired"},
        {"version": 1, "status": "retired"},
    ]


@pytest.mark.asyncio
async def test_admin_retire_of_an_unknown_tool_is_404(monkeypatch):
    monkeypatch.setattr(app_module.registry_db, "get_sessionmaker", lambda: FakeSessionmaker())
    monkeypatch.setattr(settings, "control_secret", "test-token")
    _, endpoint = admin_endpoint("/admin/tools/retire")

    response = await endpoint(FakeRequest({"name": "ghost"}, token="test-token"))

    assert response.status_code == 404
    body = json.loads(response.body)
    assert body["ok"] is False and "ghost" in body["error"]


@pytest.mark.asyncio
async def test_admin_delete_needs_confirm_for_a_hard_purge(monkeypatch):
    session = FakeRegistrySession()
    session.seed(make_tool(version=1))
    monkeypatch.setattr(app_module.registry_db, "get_sessionmaker", lambda: FakeSessionmaker(session))
    monkeypatch.setattr(settings, "control_secret", "test-token")
    _, endpoint = admin_endpoint("/admin/tools/delete")

    refused = await endpoint(FakeRequest({"name": "upper_text", "purge": True}, token="test-token"))
    assert refused.status_code == 400
    message = json.loads(refused.body)["error"]
    assert '"confirm": true' in message
    assert session.tools, "the refused purge deleted nothing"

    confirmed = _decode(
        await endpoint(FakeRequest({"name": "upper_text", "purge": True, "confirm": True}, token="test-token"))
    )
    assert confirmed == {
        "ok": True,
        "name": "upper_text",
        "version": None,
        "purged": True,
        "deleted": 1,
        "remaining": [],
    }
    assert session.tools == []
    assert session.tombstones[0]["deleted_by"] == "operator"

    bad_type = await endpoint(FakeRequest({"name": "upper_text", "purge": "yes"}, token="test-token"))
    assert bad_type.status_code == 400


@pytest.mark.asyncio
async def test_admin_delete_without_purge_only_retires(monkeypatch):
    session = FakeRegistrySession()
    session.seed(make_tool(version=1))
    monkeypatch.setattr(app_module.registry_db, "get_sessionmaker", lambda: FakeSessionmaker(session))
    monkeypatch.setattr(settings, "control_secret", "test-token")
    _, endpoint = admin_endpoint("/admin/tools/delete")

    payload = _decode(await endpoint(FakeRequest({"name": "upper_text", "purge": False}, token="test-token")))

    assert payload["purged"] is False and payload["deleted"] == 1
    assert session.statuses("upper_text") == [(1, "retired")]
    assert session.tombstones == []


@pytest.mark.asyncio
async def test_admin_endpoints_reject_a_non_json_body(monkeypatch):
    monkeypatch.setattr(app_module.registry_db, "get_sessionmaker", lambda: FakeSessionmaker())
    monkeypatch.setattr(settings, "control_secret", "test-token")
    _, endpoint = admin_endpoint("/admin/tools/delete")

    response = await endpoint(FakeRequest(None, token="test-token"))

    assert response.status_code == 400
    assert json.loads(response.body)["error"] == "body must be JSON"


def test_admin_tools_routes_are_registered():
    app = app_module.create_app()
    paths = {getattr(route, "path", None): getattr(route, "methods", set()) for route in app.routes}
    assert paths["/admin/tools"] == {"GET"}
    assert paths["/admin/tools/retire"] == {"POST"}
    assert paths["/admin/tools/delete"] == {"POST"}


def test_a_soft_delete_leaves_search_alone_but_a_retire_hides():
    """Documented behaviour check on the statement shape the fake relies on."""
    stmt = select(Tool).where(Tool.status == "active")
    assert "tools.status" in str(stmt)
    assert str(delete(Tool).where(Tool.name == "x")) == "DELETE FROM tools WHERE tools.name = :name_1"
    assert "SET status" in str(update(Tool).where(Tool.name == "x").values(status="retired"))
