"""Import every module and build both FastAPI apps (no DB, no QEMU required)."""

from __future__ import annotations

import importlib
import pkgutil
import sys

import agent
from agent.ai.app import create_app as create_ai_app
from agent.cli.main import build_parser
from agent.control.app import create_app as create_control_app
from agent.models.tool import ToolManifest
from agent.registry.seed import CORE_TOOLS

failed: list[tuple[str, str]] = []
modules = sorted(m.name for m in pkgutil.walk_packages(agent.__path__, prefix="agent."))
for name in modules:
    try:
        importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        failed.append((name, f"{type(exc).__name__}: {exc}"))

print(f"imported {len(modules) - len(failed)}/{len(modules)} modules")

ai_app = create_ai_app()
control_app = create_control_app()
ai_routes = sorted({route.path for route in ai_app.routes})  # type: ignore[attr-defined]
control_routes = sorted({route.path for route in control_app.routes})  # type: ignore[attr-defined]
print("ai routes     :", ", ".join(ai_routes))
print("control routes:", ", ".join(control_routes))

for path in ("/health", "/chat", "/sessions", "/tools", "/sandbox"):
    if path not in ai_routes:
        failed.append(("ai routes", f"missing {path}"))
for path in ("/rpc", "/sandbox/status", "/health"):
    if path not in control_routes:
        failed.append(("control routes", f"missing {path}"))

parser = build_parser()
command_lines = [
    ["serve", "ai"],
    ["serve", "control"],
    ["tools", "list"],
    ["tools", "show", "fs.read"],
    ["tools", "runs", "fs.read"],
    ["tools", "check", "manifest.json"],
    ["sandbox", "status"],
    ["sandbox", "reset", "s1"],
    ["sandbox", "invoke", "sandbox.info", "--session", "s1"],
    ["image", "verify"],
    ["image", "build"],
    ["db", "init"],
    ["db", "seed"],
    ["db", "ping"],
    ["doctor"],
    ["chat", "-m", "hi"],
]
for argv in command_lines:
    parser.parse_args(argv)
print(f"cli argument parsing verified for {len(command_lines)} command lines")

for spec in CORE_TOOLS:
    ToolManifest.model_validate(
        {
            "name": spec["name"],
            "description": spec["description"],
            "when_to_use": spec.get("when_to_use", ""),
            "tags": spec.get("tags", []),
            "params_schema": spec["params_schema"],
            "permissions": spec.get("permissions", []),
            "timeout_s": spec.get("timeout_s", 30),
            "examples": spec.get("examples", []),
            "source": spec["source"],
            "tests": [],
        }
    )
print(f"validated {len(CORE_TOOLS)} core tool manifests")

for name, error in failed:
    print("FAIL", name, error)
sys.exit(1 if failed else 0)
