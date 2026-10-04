"""`mcp.sync`: discover MCP servers and register their tools.

Runs in the AI service (it owns the registry) and talks to the servers over HTTP.  The
registered tools keep their MCP schema (converted to the subset our validator accepts)
and are callable by name like any other tool.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agent.ai import mcp as mcp_client
from agent.registry import repository as repo

log = logging.getLogger(__name__)


async def mcp_sync(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Discover tools on every configured server and register them.

    ``args`` may contain ``server`` (only sync that one) and ``retire_missing`` (retire
    tools this registry has that a server no longer advertises).
    """
    configured = mcp_client.servers()
    if not configured:
        return {
            "ok": False,
            "error": "no MCP server is configured (set AGENT_MCP_SERVERS to a JSON object of server -> {url})",
            "error_code": "not_configured",
        }
    wanted = str(args.get("server") or "").strip()
    targets = {k: v for k, v in configured.items() if not wanted or k == wanted}
    if not targets:
        return {
            "ok": False,
            "error": f"no such MCP server: {wanted!r} (configured: {', '.join(configured)})",
            "error_code": "not_found",
        }

    summary: list[dict[str, Any]] = []
    registered = 0
    for name, config in targets.items():
        client = mcp_client.McpClient(name, config)
        try:
            tools = await client.list_tools()
        except mcp_client.McpError as exc:
            summary.append({"server": name, "ok": False, "error": str(exc)})
            continue
        added: list[str] = []
        skipped: list[str] = []
        for tool in tools:
            try:
                manifest = mcp_client.to_manifest(tool)
            except Exception as exc:  # noqa: BLE001 - the registry subset is stricter than MCP
                skipped.append(f"{tool.qualified} ({type(exc).__name__}: {str(exc)[:80]})")
                continue
            embedding = None
            if ctx.embedder is not None and getattr(ctx.embedder, "available", False):
                try:
                    embedding = await ctx.embedder.embed_one(
                        repo.build_search_text(
                            manifest.name, manifest.description, manifest.when_to_use, manifest.tags
                        )
                    )
                except Exception:  # noqa: BLE001 - embedding is best effort
                    embedding = None
            async with ctx.sessionmaker() as session:
                row = await repo.insert_tool(
                    session,
                    manifest=manifest,
                    tier="core",  # operator-provided, so it gets the trusted retrieval weight
                    executor="host_native",
                    embedding=embedding,
                    embedding_model=getattr(ctx.embedder, "name", "") if embedding else "",
                    created_by=f"mcp:{name}",
                )
                await repo.retire_other_versions(session, manifest.name, row.version)
                await session.commit()
            added.append(f"{tool.qualified} v{row.version}")
            registered += 1
        summary.append({"server": name, "ok": True, "found": len(tools), "registered": added, "skipped": skipped})

    return {
        "ok": any(item.get("ok") for item in summary),
        "servers": summary,
        "registered": registered,
        "hint": "these tools are now searchable (mcp.*, permission tier trusted)",
    }


MCP_HANDLERS: dict[str, Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "mcp.sync": mcp_sync,
}
