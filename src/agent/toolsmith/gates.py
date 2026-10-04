"""The four gates a model-authored tool must pass before it is registered.

============  ==========================================================
G0 manifest   pydantic + JSON-Schema-subset validation, name rules, size
G1 static     AST allow-list check **inside the sandbox** (``py.check``)
G2 tests      the author's own test cases run **inside the sandbox**
G3 register   embedding + insert as a new active version; other versions
              of the same name are retired
============  ==========================================================

Nothing here executes tool code: G1/G2 are RPCs to the control plane, which
forwards them into the VM.  A failed gate returns structured findings so the
model can fix the source and call again.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.ai.sandbox_gateway import SandboxGateway
from agent.config import settings
from agent.embeddings import Embedder, EmbeddingUnavailable, get_embedder
from agent.models.protocol import ErrorCode, RpcError
from agent.models.tool import (
    MIN_TESTS_FOR_REGISTRATION,
    RESERVED_NAME_PREFIXES,
    ToolManifest,
    ToolPayload,
)
from agent.registry import repository as repo

log = logging.getLogger(__name__)

#: What may travel back to the model from a gate, and therefore into the session history.
#: ``tool.test`` returns one entry per case carrying the tool's own output, and ``actual``
#: is whatever the tool returned -- unbounded.  A handful of failing cases with large
#: payloads used to put hundreds of kilobytes into the conversation, and because history is
#: replayed on every later turn that is a session-wide problem, not a display one.
TOOLSMITH_CASE_MAX_CHARS = 400
TOOLSMITH_CASES_REPORTED = 10
TOOLSMITH_TEXT_MAX_CHARS = 4_000
TOOLSMITH_VIOLATIONS_REPORTED = 20
#: where the guest keeps the full per-case transcript of the last ``tool.test`` run
TOOLSMITH_TEST_LOG_PATH = "/workspace/tool-tests.log"


def _clip(value: Any, limit: int = TOOLSMITH_TEXT_MAX_CHARS) -> str:
    """A bounded rendering of anything a gate produced."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _case_summary(entry: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "name": str(entry.get("name") or "case"),
        "ok": bool(entry.get("ok")),
        "message": _clip(entry.get("message") or "", TOOLSMITH_CASE_MAX_CHARS),
    }
    if entry.get("actual") is not None:
        summary["actual"] = _clip(entry["actual"], TOOLSMITH_CASE_MAX_CHARS)
    return summary


def _slim_tests(result: dict[str, Any]) -> dict[str, Any]:
    """Counts and failing case names, never the whole transcript.

    ``log_path`` points at the guest-side file holding every line of every case, so the
    model can still go and read the detail without it entering the conversation.
    """
    cases = [entry for entry in (result.get("results") or []) if isinstance(entry, dict)]
    reported = [_case_summary(entry) for entry in cases[:TOOLSMITH_CASES_REPORTED]]
    failed_names = [str(entry.get("name") or "case") for entry in cases if not entry.get("ok")]
    slim: dict[str, Any] = {
        "passed": result.get("passed", 0),
        "failed": result.get("failed", 0),
        "failed_cases": failed_names[:TOOLSMITH_CASES_REPORTED],
        "results": reported,
    }
    if len(cases) > len(reported):
        slim["results_note"] = f"只回显前 {len(reported)} 个用例（共 {len(cases)} 个）"
    log_path = str(result.get("log_path") or "")
    if log_path:
        slim["log_path"] = log_path
    return slim


def _slim_violations(violations: Any) -> list[dict[str, Any]]:
    """Findings must stay actionable: keep the rule, the line and a bounded message."""
    out: list[dict[str, Any]] = []
    for item in (violations or [])[:TOOLSMITH_VIOLATIONS_REPORTED]:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        if "message" in entry:
            entry["message"] = _clip(entry["message"], TOOLSMITH_CASE_MAX_CHARS)
        out.append(entry)
    return out


@dataclass
class ToolsmithContext:
    session_id: str
    gateway: SandboxGateway
    sessionmaker: async_sessionmaker[AsyncSession]
    embedder: Embedder | None = None

    def embedder_or_default(self) -> Embedder:
        return self.embedder or get_embedder()


def _violation(rule: str, message: str, line: int | None = None) -> dict[str, Any]:
    return {"rule": rule, "message": message, "line": line, "severity": "error"}


def _manifest_from_args(args: dict[str, Any]) -> tuple[ToolManifest | None, list[dict[str, Any]]]:
    payload = {key: value for key, value in (args or {}).items() if value is not None}
    payload.setdefault("tests", [])
    payload.setdefault("tags", [])
    payload.setdefault("permissions", [])
    payload.setdefault("examples", [])
    try:
        manifest = ToolManifest.model_validate(payload)
    except ValidationError as exc:
        findings = []
        for error in exc.errors()[:8]:
            location = ".".join(str(part) for part in error.get("loc", ()))
            findings.append(_violation("manifest_invalid", f"{location}: {error.get('msg')}"))
        return None, findings

    reserved = [prefix for prefix in RESERVED_NAME_PREFIXES if manifest.name.startswith(prefix)]
    if reserved:
        return None, [
            _violation(
                "name_reserved",
                f"the name prefix {reserved[0]!r} is reserved for built-in tools; pick a different name "
                f"(for example 'my_{manifest.name.split('.')[-1]}')",
            )
        ]
    return manifest, []


def _tool_payload(manifest: ToolManifest) -> ToolPayload:
    return ToolPayload(
        name=manifest.name,
        version=0,
        sha256=manifest.source_sha256,
        source_b64=base64.b64encode(manifest.source.encode("utf-8")).decode("ascii"),
        permissions=list(manifest.permissions),
        entrypoint=manifest.entrypoint,
        timeout_s=manifest.timeout_s,
        executor="sandbox_python",
    )


async def _static_check(ctx: ToolsmithContext, manifest: ToolManifest) -> dict[str, Any]:
    # The operator decides how wide the import allow-list is (AGENT_TOOL_IMPORT_PROFILE,
    # mutable at runtime); the guest checker applies it, so widening needs no rebuild.
    policy = {
        "source": manifest.source,
        "permissions": list(manifest.permissions),
        "entrypoint": manifest.entrypoint,
        "profile": settings.tool_import_profile,
        "extra_modules": [item.strip() for item in (settings.tool_extra_modules or "").split(",") if item.strip()],
        "allow_open": settings.tool_allow_open,
    }
    payload = await ctx.gateway.invoke_native(
        ctx.session_id,
        "py.check",
        policy,
        timeout_s=30.0,
    )
    if not payload.ok or not isinstance(payload.result, dict):
        return {"stage": "static_check", "error": payload.error or "static check failed"}
    result = payload.result
    if not result.get("ok", False):
        return {
            "stage": "static_check",
            "error": "static analysis rejected the source",
            "violations": _slim_violations(result.get("violations")),
            "stats": result.get("stats") or {},
        }
    return {
        "ok": True,
        "stats": result.get("stats") or {},
        "violations": _slim_violations(result.get("violations")),
    }


async def _run_tests(ctx: ToolsmithContext, manifest: ToolManifest) -> dict[str, Any]:
    tool = _tool_payload(manifest)
    tests = [case.model_dump() for case in manifest.tests]
    timeout_s = float(min(300.0, max(30.0, 10.0 * len(tests) + 30.0)))
    payload = await ctx.gateway.invoke_native(
        ctx.session_id,
        "tool.test",
        {
            "tool_name": manifest.name,
            "version": tool.version,
            "sha256": tool.sha256,
            "source_b64": tool.source_b64,
            "permissions": list(manifest.permissions),
            "entrypoint": manifest.entrypoint,
            "tests": tests,
            "timeout_s": min(manifest.timeout_s, 120.0),
        },
        timeout_s=timeout_s,
    )
    if not payload.ok or not isinstance(payload.result, dict):
        return {"stage": "sandbox_tests", "error": payload.error or "test run failed"}
    result = payload.result
    outcome: dict[str, Any] = _slim_tests(result)
    if not result.get("ok"):
        outcome["error"] = "one or more sandbox test cases failed"
        outcome["stage"] = "sandbox_tests"
    else:
        outcome["ok"] = True
    return outcome


async def check(ctx: ToolsmithContext, args: dict[str, Any]) -> dict[str, Any]:
    """G0 + G1 + G2 without registering anything."""
    manifest, findings = _manifest_from_args(args)
    if manifest is None:
        return {"ok": False, "stage": "manifest", "violations": findings}
    try:
        statics = await _static_check(ctx, manifest)
        if not statics.get("ok"):
            return {"ok": False, **statics}
        tests: dict[str, Any] = {}
        if manifest.tests:
            tests = await _run_tests(ctx, manifest)
            if not tests.get("ok"):
                return {"ok": False, "stats": statics["stats"], **tests}
        return {
            "ok": True,
            "stage": "ready",
            "stats": statics["stats"],
            "warnings": _slim_violations(
                [v for v in statics.get("violations", []) if v.get("severity") == "warning"]
            ),
            "tests": {k: tests.get(k) for k in ("passed", "failed")} if tests else {"passed": 0, "failed": 0},
            "next": "call toolsmith.create with the exact same body to register it",
        }
    except RpcError as exc:
        return {"ok": False, "stage": "sandbox", "error_code": exc.label, "error": exc.message}


async def create(ctx: ToolsmithContext, args: dict[str, Any]) -> dict[str, Any]:
    """G0 + G1 + G2, then G3: embed and register a new active version."""
    manifest, findings = _manifest_from_args(args)
    if manifest is None:
        return {"ok": False, "stage": "manifest", "violations": findings}
    if len(manifest.tests) < MIN_TESTS_FOR_REGISTRATION:
        return {
            "ok": False,
            "stage": "manifest",
            "violations": [
                _violation(
                    "tests_required",
                    f"registration needs at least {MIN_TESTS_FOR_REGISTRATION} test cases "
                    f"(success, edge and error path); got {len(manifest.tests)}",
                )
            ],
        }
    try:
        statics = await _static_check(ctx, manifest)
        if not statics.get("ok"):
            return {"ok": False, **statics}
        tests = await _run_tests(ctx, manifest)
        if not tests.get("ok"):
            return {"ok": False, "stats": statics["stats"], **tests}
    except RpcError as exc:
        return {"ok": False, "stage": "sandbox", "error_code": exc.label, "error": exc.message}

    embedder = ctx.embedder_or_default()
    embedding: list[float] | None = None
    try:
        text = repo.build_search_text(manifest.name, manifest.description, manifest.when_to_use, manifest.tags)
        embedding = await embedder.embed_one(text)
    except EmbeddingUnavailable as exc:
        log.warning("registering %s without an embedding: %s", manifest.name, exc)

    async with ctx.sessionmaker() as session:
        row = await repo.insert_tool(
            session,
            manifest=manifest,
            tier="generated",
            executor="sandbox_python",
            embedding=embedding,
            embedding_model=embedder.name if embedding is not None else "",
            created_by="model",
        )
        retired = await repo.retire_other_versions(session, manifest.name, row.version)
        await session.commit()
        version = row.version

    return {
        "ok": True,
        "stage": "registered",
        "tool": {
            "name": manifest.name,
            "version": version,
            "status": "active",
            "permissions": list(manifest.permissions),
            "entrypoint": manifest.entrypoint,
            "args": manifest.params_schema,
        },
        "retired_versions": retired,
        "tests": {"passed": tests.get("passed"), "failed": tests.get("failed")},
        "embedded": embedding is not None,
        "next": f"call_tool('{manifest.name}', ...) or find it again with search_tools(...)",
    }


async def list_generated(ctx: ToolsmithContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = int((args or {}).get("limit") or 25)
    async with ctx.sessionmaker() as session:
        records = await repo.list_tools(session, tier="generated", limit=limit)
        tools = [
            {
                "name": row.name,
                "version": row.version,
                "status": row.status,
                "runs": row.runs,
                "failures": row.failures,
                "last_error": (row.last_error or "")[:200] or None,
                "description": row.description,
            }
            for row in records
        ]
    return {"ok": True, "count": len(tools), "tools": tools}


# --------------------------------------------------------------------------- #
# retirement: the model may remove only what it wrote itself
# --------------------------------------------------------------------------- #

#: Everything the agent must never be able to unregister: the resident meta tools,
#: the whole core surface (fs./exec./sandbox./net./mcp./time./host.), and the authoring
#: pipeline itself.  Seeded rows are already refused by the tier check below -- this
#: list is the belt to that braces, so a future *generated* tool that happens to shadow
#: one of these names still cannot be retired.
PROTECTED_TOOL_PREFIXES: tuple[str, ...] = ("fs.", "exec.", "sandbox.", "net.", "mcp.", "toolsmith.", "sys.", "tool.")
PROTECTED_TOOLS: frozenset[str] = frozenset({"time.now", "host.exec", "search_tools", "get_tool_schema", "call_tool"})


def _is_protected(name: str) -> bool:
    return name in PROTECTED_TOOLS or name.startswith(PROTECTED_TOOL_PREFIXES)


def _version_summary(versions: list[tuple[int, str]]) -> str:
    return ", ".join(f"v{version} ({status})" for version, status in versions) or "none"


async def retire(ctx: ToolsmithContext, args: dict[str, Any]) -> dict[str, Any]:
    """Retire (or, with ``purge=true``, hard-delete) a tool the model authored.

    Safety rule: only rows this agent wrote itself (``tier == "generated"`` and
    ``created_by == "model"``) can be removed, and never the built-in/meta tools.
    Anything seeded or core is the operator's to change, through the admin API
    (``POST /admin/tools/retire``) or the console.
    """
    payload = args or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return {
            "ok": False,
            "name": "",
            "versions_retired": [],
            "purged": False,
            "remaining": [],
            "error": "argument 'name' is required: pass the tool name you want to retire",
        }
    version = payload.get("version")
    if version is not None:
        try:
            version = int(version)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "name": name,
                "versions_retired": [],
                "purged": False,
                "remaining": [],
                "error": f"argument 'version' must be an integer, got {payload.get('version')!r}",
            }
    purge = bool(payload.get("purge", False))

    if _is_protected(name):
        return {
            "ok": False,
            "name": name,
            "versions_retired": [],
            "purged": False,
            "remaining": [],
            "error": (
                f"{name!r} is a built-in tool of the agent itself. An agent cannot retire its own "
                "capabilities (toolsmith.*, fs.*, exec.run, sandbox.*, net.*, mcp.*, time.now, "
                "host.exec). Ask the operator to remove or disable it from the console "
                "(the admin API is POST /admin/tools/retire with the operator token)."
            ),
        }

    async with ctx.sessionmaker() as session:
        rows = await repo.versions_of(session, name)
        versions = [(row.version, row.status) for row in reversed(rows)]
        if not rows:
            return {
                "ok": False,
                "name": name,
                "versions_retired": [],
                "purged": False,
                "remaining": [],
                "error": (
                    f"no tool named {name!r} is registered. toolsmith.list_mine shows the tools "
                    "you wrote; core tools are listed by search_tools."
                ),
            }
        if version is not None and version not in [row.version for row in rows]:
            return {
                "ok": False,
                "name": name,
                "versions_retired": [],
                "purged": False,
                "remaining": [row.version for row in reversed(rows)],
                "error": (
                    f"{name!r} has no version {version}. Existing version(s): {_version_summary(versions)}."
                ),
            }

        selected = [row for row in rows if version is None or row.version == version]
        not_mine = [row for row in selected if row.tier != "generated" or row.created_by != "model"]
        if not_mine:
            blocked = ", ".join(f"v{row.version} ({row.tier}/{row.created_by})" for row in not_mine)
            return {
                "ok": False,
                "name": name,
                "versions_retired": [],
                "purged": False,
                "remaining": [row.version for row in reversed(rows)],
                "error": (
                    f"refused: {name!r} {blocked} is not a tool this agent wrote, so the agent may not "
                    "remove it. Only tools created with toolsmith.create (tier 'generated', "
                    "created_by 'model') can be retired by the agent; core and seeded tools are "
                    "removed by the operator (console, or POST /admin/tools/retire with the token)."
                ),
            }

        if purge:
            changed = await repo.delete_tool(session, name, version, hard=True, deleted_by="model")
        else:
            changed = await repo.retire_tool(session, name, version)
        remaining_versions = await repo.tool_versions(session, name)
        await session.commit()

    remaining = [number for number, status in remaining_versions if status in ("active", "quarantined")]
    if changed and purge:
        remaining = []
    result: dict[str, Any] = {
        "ok": True,
        "name": name,
        "versions_retired": [version] if version is not None else [row.version for row in rows],
        "purged": bool(purge and changed),
        "remaining": remaining,
    }
    if not changed:
        result["note"] = f"{name!r} already had no removable version for this request ({_version_summary(versions)})"
    return result


TOOLSMITH_HANDLERS = {
    "toolsmith.check": check,
    "toolsmith.create": create,
    "toolsmith.list_mine": list_generated,
    "toolsmith.retire": retire,
}


def error_payload(exc: RpcError) -> dict[str, Any]:
    return {"ok": False, "error_code": exc.label or str(exc.code), "error": exc.message}


__all__ = [
    "TOOLSMITH_HANDLERS",
    "ToolsmithContext",
    "check",
    "create",
    "error_payload",
    "list_generated",
    "retire",
    "ErrorCode",
]
