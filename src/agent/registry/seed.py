"""Core tools shipped with the agent.

Every entry declares the *same* schema the model sees, so ``search_tools`` and
``get_tool_schema`` can describe native capabilities exactly like generated ones.
The model always goes through ``search_tools`` -> ``get_tool_schema`` -> ``call_tool``;
nothing here is injected into the prompt permanently except the three meta tools.

``executor`` decides where a call runs:

``sandbox_native``
    The control plane forwards the arguments to the guest RPC method with the
    same name (``fs.read`` -> guest ``fs.read``).
``host_native``
    Handled by the AI service itself (tool authoring pipeline); needs no sandbox
    but still runs every generated artefact inside the VM before registering it.
"""

from __future__ import annotations

from typing import Any

CORE_TOOLS: list[dict[str, Any]] = [
    # ------------------------------------------------------------------ fs
    {
        "name": "fs.read",
        "description": "Read a file from the sandbox workspace and return its text (or base64 when binary=true).",
        "when_to_use": "Inspect a file you or a previous tool wrote under /workspace.",
        "tags": ["file", "read", "workspace"],
        "executor": "sandbox_native",
        "permissions": [],
        "timeout_s": 20,
        "params_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "absolute path inside /workspace, or relative to /workspace"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "length": {"type": "integer", "minimum": 1, "description": "maximum bytes to return"},
                "binary": {"type": "boolean", "default": False, "description": "return base64 instead of text"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "examples": [{"arguments": {"path": "/workspace/notes.txt"}, "note": "read a text file"}],
        "source": "native handler: guest RPC `fs.read`",
    },
    {
        "name": "fs.write",
        "description": "Write (or append) a file inside /workspace, creating parent directories by default.",
        "when_to_use": "Persist data, code or reports inside the sandbox workspace.",
        "tags": ["file", "write", "workspace"],
        "executor": "sandbox_native",
        "permissions": [],
        "timeout_s": 20,
        "params_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "text": {"type": "string", "description": "UTF-8 content; mutually exclusive with data_b64"},
                "data_b64": {"type": "string", "description": "base64 content for binary files"},
                "append": {"type": "boolean", "default": False},
                "mode": {"type": "string", "default": "0644"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "examples": [{"arguments": {"path": "/workspace/out.txt", "text": "hello"}, "note": "write text"}],
        "source": "native handler: guest RPC `fs.write`",
    },
    {
        "name": "fs.list",
        "description": "List directory entries in /workspace, optionally recursive.",
        "when_to_use": "Discover what already exists in the sandbox workspace.",
        "tags": ["file", "list", "workspace"],
        "executor": "sandbox_native",
        "permissions": [],
        "timeout_s": 20,
        "params_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "/workspace"},
                "recursive": {"type": "boolean", "default": False},
                "max_entries": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 500},
            },
            "additionalProperties": False,
        },
        "examples": [{"arguments": {"path": "/workspace", "recursive": True}, "note": "tree of the workspace"}],
        "source": "native handler: guest RPC `fs.list`",
    },
    {
        "name": "fs.stat",
        "description": "Stat a path in the sandbox: existence, type, size, mode, mtime and optionally sha256.",
        "when_to_use": "Check whether a file exists or verify its hash before/after a change.",
        "tags": ["file", "stat", "workspace"],
        "executor": "sandbox_native",
        "permissions": [],
        "timeout_s": 20,
        "params_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "with_sha256": {"type": "boolean", "default": False},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "examples": [],
        "source": "native handler: guest RPC `fs.stat`",
    },
    {
        "name": "fs.mkdir",
        "description": "Create a directory inside /workspace (parents by default).",
        "when_to_use": "Prepare a directory tree before writing files.",
        "tags": ["file", "directory", "workspace"],
        "executor": "sandbox_native",
        "permissions": [],
        "timeout_s": 20,
        "params_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "parents": {"type": "boolean", "default": True},
                "mode": {"type": "string", "default": "0755"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "examples": [],
        "source": "native handler: guest RPC `fs.mkdir`",
    },
    {
        "name": "fs.remove",
        "description": "Delete a file or directory inside /workspace (recursive requires recursive=true).",
        "when_to_use": "Clean up temporary artefacts.",
        "tags": ["file", "delete", "workspace"],
        "executor": "sandbox_native",
        "permissions": [],
        "timeout_s": 20,
        "params_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "recursive": {"type": "boolean", "default": False},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "examples": [],
        "source": "native handler: guest RPC `fs.remove`",
    },
    {
        "name": "fs.move",
        "description": "Move or rename a path inside /workspace.",
        "when_to_use": "Reorganise workspace files atomically.",
        "tags": ["file", "move", "workspace"],
        "executor": "sandbox_native",
        "permissions": [],
        "timeout_s": 20,
        "params_schema": {
            "type": "object",
            "properties": {
                "src": {"type": "string"},
                "dst": {"type": "string"},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["src", "dst"],
            "additionalProperties": False,
        },
        "examples": [],
        "source": "native handler: guest RPC `fs.move`",
    },
    # ---------------------------------------------------------------- exec
    {
        "name": "exec.run",
        "description": "Run a command inside the QEMU sandbox as the unprivileged 'sandbox' user and capture stdout/stderr.",
        "when_to_use": "Run any CLI tool, script or build step. The sandbox has no network and a read-only root.",
        "tags": ["exec", "shell", "command", "process"],
        "executor": "sandbox_native",
        "permissions": [],
        "timeout_s": 60,
        "params_schema": {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "argv vector, argv[0] is the executable; e.g. [\"python3\",\"-c\",\"print(1)\"]",
                },
                "shell": {
                    "type": "boolean",
                    "default": False,
                    "description": "when true argv is joined into one bash -lc command line (quoted)",
                },
                "cwd": {"type": "string", "default": "/workspace"},
                "timeout_s": {"type": "number", "minimum": 0.5, "maximum": 300, "default": 30},
                "stdin": {"type": "string", "description": "optional text piped to stdin"},
                "env": {"type": "object", "additionalProperties": True, "description": "extra environment variables"},
                "memory_mb": {
                    "type": "integer",
                    "minimum": 32,
                    "maximum": 4096,
                    "description": "memory budget for this command (default 512 MB, enforced by RLIMIT_AS and cgroup)",
                },
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
        "examples": [
            {"arguments": {"argv": ["python3", "--version"]}, "note": "check the interpreter"},
            {"arguments": {"argv": ["ls", "-la", "/workspace"]}, "note": "list the workspace"},
        ],
        "source": "native handler: guest RPC `exec.run`",
    },
    # ------------------------------------------------------------------ MCP
    {
        "name": "mcp.sync",
        "description": "Discover the configured MCP servers and register their tools as mcp.<server>.<tool>.",
        "when_to_use": "After the operator adds an MCP server, or when an mcp.* tool seems to be missing.",
        "tags": ["mcp", "tools", "integration"],
        "executor": "host_native",
        "permissions": [],
        "timeout_s": 120,
        "params_schema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "only sync this server"},
                "retire_missing": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "examples": [{"arguments": {}, "note": "sync every configured MCP server"}],
        "source": "host handler: AI service MCP client (Streamable HTTP transport)",
    },
    # ------------------------------------------------------------ host (danger)
    {
        "name": "host.exec",
        "description": (
            "Run a command on the machine that HOSTS the control plane (not the sandbox). "
            "Requires the unrestricted permission tier, which the operator arms in the console "
            "with /perm unrestricted <phrase>."
        ),
        "when_to_use": (
            "Only when the operator has explicitly armed unrestricted mode because the task "
            "needs the real machine (installing something, driving a local tool). Everything "
            "it runs is logged; the sandbox remains the default place to work."
        ),
        "tags": ["host", "exec", "danger", "unrestricted"],
        "executor": "control_native",
        "permissions": [],
        "timeout_s": 60,
        "params_schema": {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": 'command vector, e.g. ["git", "status"]',
                },
                "cwd": {
                    "type": "string",
                    "description": "relative to the configured host workdir; must stay inside it",
                },
                "timeout_s": {"type": "number", "minimum": 0.5, "maximum": 600, "default": 60},
                "confirm": {
                    "type": "string",
                    "description": (
                        "optional; the service fills in the operator's phrase once unrestricted "
                        "mode is armed. Passing it yourself is refused unless it matches."
                    ),
                },
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
        "examples": [
            {
                "arguments": {"argv": ["git", "status", "--short"], "confirm": "<phrase>"},
                "note": "requires unrestricted tier; the operator arms it with /perm unrestricted <phrase>",
            }
        ],
        "source": "control-plane handler: agent.control.host.exec",
    },
    # ------------------------------------------------------------ built-ins
    {
        "name": "fs.pull",
        "description": (
            "Copy a file the sandbox produced onto the host machine, into the repo's "
            "var\\pulled directory, and return its host path (the operator opens it with /get)."
        ),
        "when_to_use": (
            "Whenever the result of the work IS a file or an image (a plot, a report, a "
            "screenshot) and the operator should be able to open it from Windows. Reads "
            "through the sandbox gateway and writes through the control plane, so the AI "
            "service never touches the host filesystem itself. Cap: 8 MB. Never overwrites "
            "an existing file unless overwrite=true."
        ),
        "tags": ["file", "host", "artifact", "image", "download"],
        "executor": "host_native",
        "permissions": [],
        "timeout_s": 180,
        "params_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "sandbox path of the file, inside /workspace (the only writable path)",
                },
                "dest": {
                    "type": "string",
                    "description": (
                        "optional host destination, relative to var\\pulled; "
                        "defaults to the file's own name. No '..', no absolute path."
                    ),
                },
                "overwrite": {
                    "type": "boolean",
                    "default": False,
                    "description": "replace the host file when it already exists",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "examples": [
            {"arguments": {"path": "/workspace/plot.png"}, "note": "lands in var\\pulled\\plot.png"},
            {
                "arguments": {"path": "/workspace/report.md", "dest": "runs/2024/report.md"},
                "note": "subdirectories under var\\pulled are created",
            },
        ],
        "source": "host handler: AI service pull reader + control-plane handler agent.control.host.pull",
    },
    {
        "name": "time.now",
        "description": "Current date and time (UTC, plus the service's local zone and epoch seconds).",
        "when_to_use": (
            "Whenever the task depends on the current time or date. The sandbox has no "
            "NTP and no NIC, so this is the only reliable clock."
        ),
        "tags": ["time", "clock", "date", "builtin"],
        "executor": "host_native",
        "permissions": [],
        "timeout_s": 10,
        "params_schema": {
            "type": "object",
            "properties": {
                "tz_offset_hours": {
                    "type": "number",
                    "minimum": -14,
                    "maximum": 14,
                    "description": "optional: report the time in this UTC offset (e.g. 8 for Beijing)",
                }
            },
            "additionalProperties": False,
        },
        "examples": [
            {"arguments": {}, "note": "UTC + local + epoch"},
            {"arguments": {"tz_offset_hours": 8}, "note": "Beijing time"},
        ],
        "source": "host handler: AI service builtins (agent.ai.builtins)",
    },
    # ------------------------------------------------------------- network
    # These two run in the AI service (the sandbox stays without a NIC).  They are
    # hidden from search_tools and refused while AGENT_NET_ENABLED is off.
    {
        "name": "net.fetch",
        "description": "Download a URL (http/https) and save the body into the sandbox workspace.",
        "when_to_use": (
            "Fetch a file, page or API payload the sandbox cannot reach itself. "
            "Requires the operator to enable networking and allowlist the host."
        ),
        "tags": ["net", "http", "download", "web"],
        "executor": "host_native",
        "permissions": [],
        "timeout_s": 60,
        "params_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "absolute http(s) URL"},
                "dest": {
                    "type": "string",
                    "description": "workspace path to save to (default /workspace/downloads/<name>)",
                },
                "max_bytes": {"type": "integer", "minimum": 1, "description": "override the per-request cap"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "examples": [
            {"arguments": {"url": "https://example.com/data.json"}, "note": "saves into /workspace/downloads/"},
        ],
        "source": "host handler: AI service HTTP client (firewalled)",
    },
    {
        "name": "net.ping",
        "description": "TCP reachability probe: does host:port answer, and how fast?",
        "when_to_use": (
            "To check whether a service is up before fetching from it, or to explain a "
            "network failure. Runs in the AI service (the sandbox has no NIC) and obeys "
            "the same allowlist, port list and private-address rules as net.fetch."
        ),
        "tags": ["net", "ping", "probe", "reachability"],
        "executor": "host_native",
        "permissions": [],
        "timeout_s": 30,
        "params_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "hostname or IP (must be allowlisted)"},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535, "default": 443},
                "timeout_s": {"type": "number", "minimum": 0.5, "maximum": 30, "default": 5},
            },
            "required": ["host"],
            "additionalProperties": False,
        },
        "examples": [
            {"arguments": {"host": "example.com", "port": 443}, "note": "must pass the allowlist"},
        ],
        "source": "host handler: AI service HTTP client (firewalled)",
    },
    {
        "name": "net.http",
        "description": "Send an arbitrary HTTP request (method, headers, body) through the same firewall.",
        "when_to_use": "Call a JSON API: POST a body, read the response, optionally save it into the workspace.",
        "tags": ["net", "http", "api", "post"],
        "executor": "host_native",
        "permissions": [],
        "timeout_s": 60,
        "params_schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "description": "GET/POST/PUT/PATCH/DELETE/HEAD"},
                "url": {"type": "string"},
                "headers": {"type": "object", "additionalProperties": True},
                "body": {"type": "string", "description": "request body as text"},
                "dest": {"type": "string", "description": "optional workspace path for the response body"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "examples": [
            {
                "arguments": {"method": "POST", "url": "https://example.com/api", "body": '{"q": 1}'},
                "note": "requires the host to be allowlisted",
            },
        ],
        "source": "host handler: AI service HTTP client (firewalled)",
    },
    # ------------------------------------------------------------- sandbox
    {
        "name": "sandbox.info",
        "description": "Report sandbox facts: kernel, python version, uid, read-only root state, network interfaces, counters.",
        "when_to_use": "Verify your assumptions about the sandbox environment before running something risky.",
        "tags": ["sandbox", "diagnostics", "limits"],
        "executor": "sandbox_native",
        "permissions": [],
        "timeout_s": 15,
        "params_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "examples": [],
        "source": "native handler: guest RPC `sandbox.info`",
    },
    {
        "name": "sandbox.reset",
        "description": "Delete everything under /workspace (installed tools are kept unless keep_tools=false).",
        "when_to_use": "Start from a clean workspace inside the current session.",
        "tags": ["sandbox", "reset", "workspace"],
        "executor": "sandbox_native",
        "permissions": [],
        "timeout_s": 30,
        "params_schema": {
            "type": "object",
            "properties": {"keep_tools": {"type": "boolean", "default": True}},
            "additionalProperties": False,
        },
        "examples": [],
        "source": "native handler: guest RPC `sandbox.reset`",
    },
    # ------------------------------------------------------------ toolsmith
    {
        "name": "toolsmith.check",
        "description": (
            "Statically check a candidate Python tool and run its test cases inside the sandbox WITHOUT registering it. "
            "Returns AST violations and per-test results."
        ),
        "when_to_use": "Iterate on a new tool until it is clean, then call toolsmith.create with the same body.",
        "tags": ["toolsmith", "authoring", "lint", "test"],
        "executor": "host_native",
        "permissions": [],
        "timeout_s": 120,
        "params_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "tool name, ^[a-z][a-z0-9_]{2,40}$"},
                "description": {"type": "string"},
                "when_to_use": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "params_schema": {"type": "object", "additionalProperties": True},
                "permissions": {"type": "array", "items": {"type": "string"}},
                "source": {"type": "string", "description": "python source defining run(args) -> dict"},
                "tests": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            },
            "required": ["name", "description", "params_schema", "source"],
            "additionalProperties": False,
        },
        "examples": [],
        "source": "host handler: toolsmith/gates.py check_gates()",
    },
    {
        "name": "toolsmith.create",
        "description": (
            "Full tool authoring pipeline: manifest validation, AST static check, sandbox test run, then registration "
            "as a new active version. Requires at least 3 test cases covering success, edge and error paths."
        ),
        "when_to_use": "After toolsmith.check reports no violations and all tests pass.",
        "tags": ["toolsmith", "authoring", "register", "create_tool"],
        "executor": "host_native",
        "permissions": [],
        "timeout_s": 180,
        "params_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "when_to_use": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "params_schema": {"type": "object", "additionalProperties": True},
                "permissions": {"type": "array", "items": {"type": "string"}},
                "source": {"type": "string"},
                "tests": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            },
            "required": ["name", "description", "params_schema", "source", "tests"],
            "additionalProperties": False,
        },
        "examples": [],
        "source": "host handler: toolsmith/gates.py register_gates()",
    },
    {
        "name": "toolsmith.list_mine",
        "description": "List model-authored tools with their status (active/quarantined), version and failure counts.",
        "when_to_use": "Review what you already built before authoring something similar.",
        "tags": ["toolsmith", "authoring", "list", "inventory"],
        "executor": "host_native",
        "permissions": [],
        "timeout_s": 20,
        "params_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25}},
            "additionalProperties": False,
        },
        "examples": [],
        "source": "host handler: toolsmith/gates.py list_generated()",
    },
    {
        "name": "toolsmith.retire",
        "description": (
            "Retire a tool you wrote yourself so it disappears from search_tools and get_tool_schema, "
            "or delete it permanently with purge=true. One version or all of them."
        ),
        "when_to_use": (
            "Remove a tool you wrote that is wrong, superseded or unused. Only your own tools "
            "(created by toolsmith.create) can be removed: core/built-in tools "
            "(toolsmith.*, fs.*, exec.run, sandbox.*, net.*, mcp.*, time.now, host.exec) cannot be "
            "removed by the agent -- the operator does that from the console."
        ),
        "tags": ["toolsmith", "authoring", "retire", "delete", "cleanup"],
        "executor": "host_native",
        "permissions": [],
        "timeout_s": 15,
        "params_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "name of the tool you wrote"},
                "version": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "retire only this version; omit to retire every version of the name",
                },
                "purge": {
                    "type": "boolean",
                    "default": False,
                    "description": "true deletes the rows permanently (the run history is kept); false only hides them",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "examples": [
            {"arguments": {"name": "upper_text"}, "note": "hide every version of a tool you wrote"},
            {"arguments": {"name": "upper_text", "version": 2}, "note": "hide just version 2"},
            {"arguments": {"name": "upper_text", "purge": True}, "note": "delete it for good"},
        ],
        "source": "host handler: toolsmith/gates.py retire()",
    },
]

CORE_TOOL_NAMES: tuple[str, ...] = tuple(spec["name"] for spec in CORE_TOOLS)
SANDBOX_NATIVE_TOOLS: tuple[str, ...] = tuple(
    spec["name"] for spec in CORE_TOOLS if spec["executor"] == "sandbox_native"
)
HOST_NATIVE_TOOLS: tuple[str, ...] = tuple(spec["name"] for spec in CORE_TOOLS if spec["executor"] == "host_native")
