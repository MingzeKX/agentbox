"""Architectural guard: the AI service must never touch host files or processes.

These tests parse (never execute) every module that runs inside the AI service
and fail if one of them grows the ability to:

* import a process/OS/device module (``subprocess``, ``socket``, ``ctypes``,
  ``shutil``, ``os`` ...)
* call ``open()`` or read/write a path directly
* import ``agent.control.*`` (the only subsystem allowed to spawn QEMU)

Why it matters: the AI service is the component driven by LLM output.  Tool
execution, file access and process creation all belong to the sandbox (through
the control plane).  If this test starts failing, that boundary has been broken.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src" / "agent"

#: modules that may never be imported by the AI service side
FORBIDDEN_IMPORTS = {
    "subprocess",
    "ctypes",
    "multiprocessing",
    "pty",
    "fcntl",
    "termios",
    "resource",
    "signal",
    "socket",
    "selectors",
    "shutil",
    "importlib",
    "runpy",
    "webbrowser",
    "pickle",
    "marshal",
}

#: module roots that are scanned
SCANNED_PACKAGES = ("ai", "registry", "toolsmith", "models")

#: files exempted from the "no direct path access" rule, with the reason
PATH_ACCESS_ALLOWED = {
    "ai/agent_loop.py": "reads its own bundled system prompt, not agent-chosen paths",
    "ai/personas.py": (
        "reads the bundled/operator persona directories only; names are sanitised to "
        "[a-z0-9_-]{1,32} so a model-supplied string cannot escape them"
    ),
    "registry/tables.py": "declares SQLAlchemy tables; no filesystem access",
}

#: files exempted from the whole scan
SCAN_EXEMPT = {
    "config.py": "only locates qemu binaries (is_file/which); it never runs or reads them",
}

PATH_METHODS = {"read_text", "write_text", "read_bytes", "write_bytes", "unlink", "mkdir", "iterdir"}


def scanned_files() -> list[Path]:
    files: list[Path] = []
    for package in SCANNED_PACKAGES:
        files.extend(sorted((SRC / package).rglob("*.py")))
    files.append(SRC / "embeddings.py")
    # The ASR wrapper is loaded by the AI service and decodes client-supplied bytes:
    # it must obey the same no-filesystem/no-process/no-network rules as the rest.
    files.append(SRC / "asr.py")
    return [path for path in files if path.name not in SCAN_EXEMPT]


def relative(path: Path) -> str:
    return str(path.relative_to(SRC)).replace("\\", "/")


@pytest.mark.parametrize("path", scanned_files(), ids=relative)
def test_no_process_or_device_imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported.add(node.module.split(".")[0])
    offenders = sorted(imported & FORBIDDEN_IMPORTS)
    assert not offenders, (
        f"{relative(path)} imports {offenders}. The AI service must not be able to "
        "spawn processes or talk to devices; route it through the control plane instead."
    )


@pytest.mark.parametrize("path", scanned_files(), ids=relative)
def test_no_direct_control_plane_internals(path: Path):
    """The AI service may only reach a sandbox through its HTTP gateway."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders.extend(alias.name for alias in node.names if alias.name.startswith("agent.control"))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("agent.control"):
            offenders.append(node.module)
    assert not offenders, (
        f"{relative(path)} imports {offenders}; only agent.ai.sandbox_gateway may talk to the control plane, "
        "and it must do so over HTTP"
    )


@pytest.mark.parametrize("path", scanned_files(), ids=relative)
def test_no_direct_file_access(path: Path):
    rel = relative(path)
    if rel in PATH_ACCESS_ALLOWED:
        pytest.skip(f"allowed: {PATH_ACCESS_ALLOWED[rel]}")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open":
                problems.append(f"line {node.lineno}: open()")
            if isinstance(func, ast.Attribute) and func.attr in PATH_METHODS:
                problems.append(f"line {node.lineno}: .{func.attr}()")
    assert not problems, (
        f"{rel} touches the filesystem directly ({'; '.join(problems)}). "
        "The AI service must ask the sandbox for files."
    )


def test_gateway_is_the_single_http_entry_point():
    """Only the gateway and the LLM client may perform network calls."""
    allowed = {
        "ai/sandbox_gateway.py",
        "ai/llm.py",
        # firewalled outbound client (allowlist + SSRF checks + byte cap): it lives in
        # the AI service precisely so the sandbox itself needs no NIC
        "ai/net.py",
        # MCP servers are spoken to over Streamable HTTP; the client never spawns a
        # process (stdio servers must be exposed over HTTP instead)
        "ai/mcp.py",
        "embeddings.py",
        "ai/app.py",
        "cli/main.py",
    }
    offenders: list[str] = []
    for path in scanned_files():
        rel = relative(path)
        if rel in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in {"httpx", "requests", "urllib", "http"}:
                        offenders.append(f"{rel}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in {"httpx", "requests", "urllib", "http"}:
                    offenders.append(f"{rel}: from {node.module}")
    assert not offenders, "unexpected outbound network client: " + ", ".join(offenders)


def test_the_scan_actually_covers_the_ai_service():
    files = scanned_files()
    names = {relative(path) for path in files}
    assert "ai/agent_loop.py" in names
    assert "ai/metacalls.py" in names
    assert "ai/app.py" in names
    assert "registry/service.py" in names
    assert "toolsmith/gates.py" in names
    assert len(files) >= 15


def test_control_plane_is_the_only_place_that_spawns_processes():
    """Every ``asyncio.create_subprocess_exec`` in the tree lives in the control plane."""
    offenders: list[str] = []
    for path in sorted((SRC).rglob("*.py")):
        if "control" in path.parts:
            continue
        if "cli" in path.parts:
            continue  # the operator CLI may drive the build script
        text = path.read_text(encoding="utf-8")
        for needle in ("create_subprocess_exec", "os.system(", "os.popen("):
            if needle in text:
                offenders.append(f"{relative(path)}: {needle}")
    assert not offenders, "process creation outside the control plane: " + ", ".join(offenders)
