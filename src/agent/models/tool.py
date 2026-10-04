"""Shared data models: tool registry objects and the JSON-RPC wire contract.

Import order note: this module is deliberately dependency free (pydantic only) so
that the registry, the AI service, the control plane and the test-suite can all
import it without pulling in SQLAlchemy or QEMU.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
TOOL_NAME_MIN = 3
TOOL_NAME_MAX = 41
MAX_NAME_SEGMENT = 31

#: name prefixes reserved for the built-in tools; model-authored tools may not use them
RESERVED_NAME_PREFIXES: tuple[str, ...] = ("fs.", "exec.", "sandbox.", "toolsmith.", "sys.", "tool.", "py.")
MAX_DESCRIPTION = 400
MAX_WHEN_TO_USE = 400
MAX_TAGS = 8
MAX_TAG_LEN = 24
MAX_EXAMPLES = 4
MIN_TESTS_FOR_REGISTRATION = 3

Permission = Literal["fs.read", "fs.write", "exec", "exec.shell"]
ALL_PERMISSIONS: tuple[str, ...] = ("fs.read", "fs.write", "exec", "exec.shell")

ToolStatus = Literal["active", "quarantined", "retired"]
ToolTier = Literal["core", "generated"]
#: host_native runs in the AI service; control_native is forwarded to the control
#: plane (the machine that hosts the VMs) -- currently only host.exec, gated hard
ToolExecutor = Literal["meta", "host_native", "control_native", "sandbox_native", "sandbox_python"]

RESIDENT_META_TOOLS: tuple[str, ...] = ("search_tools", "get_tool_schema", "call_tool")

SCHEMA_TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")
SCHEMA_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "items",
        "enum",
        "const",
        "description",
        "default",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
        "additionalProperties",
        "title",
    }
)


class SchemaViolation(BaseModel):
    path: str
    message: str


def validate_params_schema(schema: dict[str, Any]) -> list[SchemaViolation]:
    """Validate that ``schema`` is inside the JSON-Schema subset we support.

    Subset: object roots, ``properties``/``required``/``items`` recursion, scalar
    constraints, ``enum``/``const``, ``additionalProperties: false``.  Anything
    else (``$ref``, ``oneOf``, ``allOf``, ``anyOf``, ``not``, ``patternProperties``,
    ``definitions``, ``$defs``) is rejected because the tool runner only enforces
    what it understands.
    """
    problems: list[SchemaViolation] = []

    def walk(node: Any, path: str, depth: int) -> None:
        if depth > 8:
            problems.append(SchemaViolation(path=path, message="schema nested too deeply (max 8)"))
            return
        if not isinstance(node, dict):
            problems.append(SchemaViolation(path=path, message="schema node must be an object"))
            return
        unknown = sorted(set(node) - SCHEMA_KEYS)
        if unknown:
            problems.append(
                SchemaViolation(path=path, message=f"unsupported schema keyword(s): {', '.join(unknown)}")
            )
        node_type = node.get("type")
        if node_type is None and "enum" not in node and "const" not in node:
            problems.append(SchemaViolation(path=path, message="missing 'type'"))
        elif node_type is not None:
            if isinstance(node_type, list):
                problems.append(
                    SchemaViolation(path=path, message="union types (list) are not supported; pick one type")
                )
            elif node_type not in SCHEMA_TYPES:
                problems.append(SchemaViolation(path=path, message=f"unsupported type {node_type!r}"))
        props = node.get("properties")
        if props is not None:
            if node_type != "object":
                problems.append(SchemaViolation(path=path, message="'properties' requires type 'object'"))
            if not isinstance(props, dict):
                problems.append(SchemaViolation(path=path, message="'properties' must be an object"))
            else:
                for key, sub in props.items():
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", str(key)):
                        problems.append(
                            SchemaViolation(path=f"{path}.{key}", message="property name must match [A-Za-z_][A-Za-z0-9_]*")
                        )
                    walk(sub, f"{path}.{key}", depth + 1)
        required = node.get("required")
        if required is not None:
            if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
                problems.append(SchemaViolation(path=path, message="'required' must be a list of strings"))
            elif props is not None and isinstance(props, dict):
                missing = [r for r in required if r not in props]
                if missing:
                    problems.append(
                        SchemaViolation(path=path, message=f"'required' lists unknown propert(ies): {', '.join(missing)}")
                    )
        items = node.get("items")
        if items is not None:
            if node_type != "array":
                problems.append(SchemaViolation(path=path, message="'items' requires type 'array'"))
            walk(items, f"{path}[]", depth + 1)
        if "enum" in node and not isinstance(node["enum"], list):
            problems.append(SchemaViolation(path=path, message="'enum' must be a list"))
        add = node.get("additionalProperties")
        if add is not None and not isinstance(add, bool):
            problems.append(
                SchemaViolation(path=path, message="'additionalProperties' must be a boolean in this subset")
            )

    if not isinstance(schema, dict) or schema.get("type") != "object":
        return [SchemaViolation(path="$", message="root schema must be an object with type 'object'")]
    walk(schema, "$", 0)
    return problems


def summarize_schema(schema: dict[str, Any]) -> str:
    """One-line ``arg: type`` summary used in search results (keeps context small)."""
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    parts: list[str] = []
    for name, spec in props.items():
        t = spec.get("type", "any") if isinstance(spec, dict) else "any"
        if isinstance(spec, dict) and spec.get("enum"):
            t = "|".join(str(x) for x in spec["enum"][:4])
        parts.append(f"{name}:{t}{'' if name in required else '?'}")
    return ", ".join(parts) if parts else "(no arguments)"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_hex(text.encode("utf-8"))


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


# --------------------------------------------------------------------------- #
# tool registry objects
# --------------------------------------------------------------------------- #


class ToolExample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arguments: dict[str, Any] = Field(default_factory=dict)
    note: str = ""

    @field_validator("note")
    @classmethod
    def _short(cls, v: str) -> str:
        if len(v) > 200:
            raise ValueError("example note must be <= 200 chars")
        return v


class Expect(BaseModel):
    """Declarative assertion for one test case.

    Exactly one of ``equals`` / ``contains`` / ``raises`` / ``is_true`` must be set.
    """

    model_config = ConfigDict(extra="forbid")
    equals: Any = None
    contains: dict[str, Any] | None = None
    raises: str | None = None
    is_true: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Expect:
        set_count = sum(
            [
                self.equals is not None,
                self.contains is not None,
                self.raises is not None,
                self.is_true is not None,
            ]
        )
        if set_count != 1:
            raise ValueError("exactly one of equals/contains/raises/is_true must be provided")
        return self


class ToolTestCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=80)]
    args: dict[str, Any] = Field(default_factory=dict)
    expect: Expect
    timeout_s: float | None = Field(default=None, gt=0, le=300)


class ToolManifest(BaseModel):
    """A complete tool definition as authored by the model."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: Annotated[str, Field(min_length=10, max_length=MAX_DESCRIPTION)]
    when_to_use: Annotated[str, Field(min_length=5, max_length=MAX_WHEN_TO_USE)] = ""
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    params_schema: dict[str, Any]
    permissions: list[Permission] = Field(default_factory=list)
    entrypoint: str = "run"
    timeout_s: float = Field(default=120.0, gt=0, le=600)
    examples: list[ToolExample] = Field(default_factory=list, max_length=MAX_EXAMPLES)
    source: Annotated[str, Field(min_length=1)]
    tests: list[ToolTestCase] = Field(default_factory=list)

    # -------------------------------------------------------------- validators
    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not TOOL_NAME_RE.match(v):
            raise ValueError(
                "name must match ^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*$ "
                "(lowercase letters, digits, underscores; dots separate name segments)"
            )
        if not (TOOL_NAME_MIN <= len(v) <= TOOL_NAME_MAX):
            raise ValueError(f"name must be {TOOL_NAME_MIN}-{TOOL_NAME_MAX} characters long")
        if any(len(segment) > MAX_NAME_SEGMENT for segment in v.split(".")):
            raise ValueError(f"each dot separated segment must be at most {MAX_NAME_SEGMENT} characters")
        if v in RESIDENT_META_TOOLS:
            raise ValueError(f"'{v}' is a resident meta-tool name and cannot be redefined")
        return v

    @field_validator("entrypoint")
    @classmethod
    def _entrypoint(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", v):
            raise ValueError("entrypoint must be a valid python identifier")
        return v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for tag in v:
            t = tag.strip().lower()
            if not t or len(t) > MAX_TAG_LEN or not re.fullmatch(r"[a-z0-9_\-]+", t):
                raise ValueError(f"invalid tag {tag!r}: use [a-z0-9_-], max {MAX_TAG_LEN} chars")
            if t not in out:
                out.append(t)
        return out

    @field_validator("permissions")
    @classmethod
    def _perms(cls, v: list[str]) -> list[str]:
        bad = [p for p in v if p not in ALL_PERMISSIONS]
        if bad:
            raise ValueError(f"unknown permission(s) {bad}; allowed: {', '.join(ALL_PERMISSIONS)}")
        if "exec.shell" in v and "exec" not in v:
            raise ValueError("'exec.shell' requires 'exec'")
        # de-duplicate, keep stable order
        return [p for p in ALL_PERMISSIONS if p in set(v)]

    @field_validator("params_schema")
    @classmethod
    def _schema(cls, v: dict[str, Any]) -> dict[str, Any]:
        problems = validate_params_schema(v)
        if problems:
            raise ValueError("; ".join(f"{p.path}: {p.message}" for p in problems[:6]))
        return v

    @field_validator("source")
    @classmethod
    def _source(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("source must not contain NUL bytes")
        return v

    # ----------------------------------------------------------------- helpers
    @property
    def source_sha256(self) -> str:
        return sha256_text(self.source)

    def compact(self) -> dict[str, Any]:
        """Small representation used when injecting a discovered tool into context."""
        return {
            "name": self.name,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "args": summarize_schema(self.params_schema),
            "permissions": self.permissions,
            "tags": self.tags,
        }


class ToolSummary(BaseModel):
    """Search hit returned by ``search_tools``."""

    name: str
    version: int
    description: str
    when_to_use: str
    args_brief: str
    permissions: list[str]
    tags: list[str]
    tier: ToolTier
    executor: ToolExecutor
    score: float = 0.0


class ToolSchemaView(BaseModel):
    """Full definition returned by ``get_tool_schema``."""

    name: str
    version: int
    description: str
    when_to_use: str
    params_schema: dict[str, Any]
    permissions: list[str]
    tags: list[str]
    timeout_s: float
    examples: list[ToolExample]
    executor: ToolExecutor
    status: ToolStatus
    tier: ToolTier
    source: str | None = None
    runs: int = 0
    failures: int = 0
    last_error: str | None = None


class ToolRecord(BaseModel):
    """Row of the ``tools`` table (never sent to the model verbatim)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    version: int
    status: ToolStatus
    tier: ToolTier
    executor: ToolExecutor
    description: str
    when_to_use: str
    tags: list[str]
    params_schema: dict[str, Any]
    permissions: list[str]
    timeout_s: float
    entrypoint: str
    examples: list[ToolExample]
    source: str
    source_sha256: str
    embedding_model: str
    created_at: str
    runs: int = 0
    failures: int = 0
    last_error: str | None = None

    def schema_view(self, include_source: bool = False) -> ToolSchemaView:
        return ToolSchemaView(
            name=self.name,
            version=self.version,
            description=self.description,
            when_to_use=self.when_to_use,
            params_schema=self.params_schema,
            permissions=self.permissions,
            tags=self.tags,
            timeout_s=self.timeout_s,
            examples=self.examples,
            executor=self.executor,
            status=self.status,
            tier=self.tier,
            source=self.source if include_source else None,
            runs=self.runs,
            failures=self.failures,
            last_error=self.last_error,
        )

    def payload(self) -> ToolPayload:
        return ToolPayload(
            name=self.name,
            version=self.version,
            sha256=self.source_sha256,
            source_b64=b64e(self.source.encode("utf-8")),
            permissions=list(self.permissions),
            entrypoint=self.entrypoint,
            timeout_s=self.timeout_s,
            executor=self.executor,
        )


class ToolPayload(BaseModel):
    """Everything the control plane needs to run a generated tool inside a VM.

    The AI service resolves this from PostgreSQL and hands it over as data; the
    control plane re-verifies ``sha256`` before writing anything into the VM.
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    version: int
    sha256: str
    source_b64: str
    permissions: list[Permission] = Field(default_factory=list)
    entrypoint: str = "run"
    timeout_s: float = 30.0
    executor: ToolExecutor = "sandbox_python"

    @property
    def source(self) -> str:
        return b64d(self.source_b64).decode("utf-8")


class ToolCallResult(BaseModel):
    ok: bool
    tool: str
    version: int | None = None
    result: Any | None = None
    error: str | None = None
    error_code: str | None = None
    duration_ms: int = 0
    truncated: bool = False
    trace_id: str = ""


# --------------------------------------------------------------------------- #
# agent <-> control plane
# --------------------------------------------------------------------------- #


class SandboxAcquireParams(BaseModel):
    session_id: str
    reuse: bool = True


class SandboxHandle(BaseModel):
    session_id: str
    vm_id: str | None
    state: Literal["ready", "booting", "absent"]
    reused: bool = False


class SandboxInvokeParams(BaseModel):
    """One tool invocation, executed inside the sandbox VM."""

    session_id: str
    kind: Literal["native", "python"]
    method: str | None = None
    tool: ToolPayload | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float | None = None

    @model_validator(mode="after")
    def _shape(self) -> SandboxInvokeParams:
        if self.kind == "native" and not self.method:
            raise ValueError("kind='native' requires 'method'")
        if self.kind == "python" and self.tool is None:
            raise ValueError("kind='python' requires 'tool'")
        return self


class SandboxInvokeResult(BaseModel):
    ok: bool
    result: Any | None = None
    error_code: str | None = None
    error: str | None = None
    duration_ms: int = 0
    vm_id: str | None = None
    trace_id: str = ""


class VmStatus(BaseModel):
    vm_id: str
    session_id: str | None
    state: Literal["booting", "ready", "busy", "stopped", "failed"]
    accel: str
    booted_at: str | None = None
    uptime_s: float = 0.0
    commands_run: int = 0


class PoolStatus(BaseModel):
    accel: str
    pool_size: int
    warm: int
    active: int
    vms: list[VmStatus]
    image_problems: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# guest RPC payloads (validated by the guest defensively, mirrored here)
# --------------------------------------------------------------------------- #


class SysHelloParams(BaseModel):
    token: str
    nonce: str
    client: str = "control-plane"
    protocol: int = 1


class SysHelloResult(BaseModel):
    ok: bool
    vm_id: str
    hostname: str
    kernel: str
    python: str
    uid: int
    protocol: int
    wired: bool = False
    capabilities: list[str] = Field(default_factory=list)


class ExecRunParams(BaseModel):
    argv: list[str]
    shell: bool = False
    cwd: str = "/workspace"
    env: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = Field(default=120.0, gt=0, le=600)
    stdin: str | None = None
    max_output_bytes: int = Field(default=1_000_000, gt=0, le=8_000_000)
    memory_mb: int | None = Field(default=None, ge=32, le=4096)
    user: str | None = None

    @field_validator("argv", mode="before")
    @classmethod
    def _argv(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [v]
        return v

    @field_validator("argv")
    @classmethod
    def _argv_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("argv must contain at least one element")
        if len(v) > 256:
            raise ValueError("argv is limited to 256 elements")
        for item in v:
            if not isinstance(item, str) or "\x00" in item:
                raise ValueError("every argv element must be a string without NUL bytes")
        if sum(len(x) for x in v) > 32768:
            raise ValueError("argv is limited to 32 KiB in total")
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 64:
            raise ValueError("at most 64 environment variables")
        for key, value in v.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key):
                raise ValueError(f"invalid environment variable name {key!r}")
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError(f"invalid value for {key}")
        return v


class ExecRunResult(BaseModel):
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    truncated: bool = False
    signal: int | None = None
    oom: bool = False
    killed_by_limit: str | None = None


class FsReadParams(BaseModel):
    path: str
    offset: int = Field(default=0, ge=0)
    length: int | None = Field(default=None, gt=0)
    binary: bool = False
    max_bytes: int = Field(default=1_000_000, gt=0, le=8_000_000)


class FsReadResult(BaseModel):
    path: str
    size: int
    sha256: str
    truncated: bool = False
    text: str | None = None
    data_b64: str | None = None


class FsWriteParams(BaseModel):
    path: str
    text: str | None = None
    data_b64: str | None = None
    mode: str = "0644"
    create_dirs: bool = True
    append: bool = False

    @model_validator(mode="after")
    def _one_payload(self) -> FsWriteParams:
        if (self.text is None) == (self.data_b64 is None):
            raise ValueError("provide exactly one of 'text' or 'data_b64'")
        return self


class FsWriteResult(BaseModel):
    path: str
    bytes_written: int
    sha256: str


class FsEntry(BaseModel):
    path: str
    name: str
    type: Literal["file", "dir", "symlink", "other"]
    size: int = 0
    mode: str = ""
    mtime: float = 0.0


class FsListParams(BaseModel):
    path: str = "/workspace"
    recursive: bool = False
    max_entries: int = Field(default=2000, gt=0, le=20000)
    include_hidden: bool = True


class FsListResult(BaseModel):
    path: str
    entries: list[FsEntry]
    truncated: bool = False


class FsStatParams(BaseModel):
    path: str
    with_sha256: bool = False


class FsStatResult(BaseModel):
    path: str
    exists: bool
    type: Literal["file", "dir", "symlink", "other"] | None = None
    size: int = 0
    mode: str = ""
    mtime: float = 0.0
    uid: int = 0
    gid: int = 0
    sha256: str | None = None


class FsPathParams(BaseModel):
    path: str


class FsMkdirParams(BaseModel):
    path: str
    parents: bool = True
    mode: str = "0755"


class FsRemoveParams(BaseModel):
    path: str
    recursive: bool = False


class FsMoveParams(BaseModel):
    src: str
    dst: str
    overwrite: bool = False


class SimpleResult(BaseModel):
    ok: bool = True
    detail: str = ""


class Violation(BaseModel):
    rule: str
    message: str
    line: int | None = None
    col: int | None = None
    severity: Literal["error", "warning"] = "error"


class CheckStats(BaseModel):
    loc: int = 0
    imports: list[str] = Field(default_factory=list)
    functions: list[str] = Field(default_factory=list)
    has_entrypoint: bool = False
    max_nesting: int = 0


class PyCheckParams(BaseModel):
    source: str
    permissions: list[str] = Field(default_factory=list)
    entrypoint: str = "run"


class PyCheckResult(BaseModel):
    ok: bool
    violations: list[Violation] = Field(default_factory=list)
    stats: CheckStats = Field(default_factory=CheckStats)
    source_sha256: str = ""


class PyRunParams(BaseModel):
    tool_name: str
    version: int
    sha256: str
    source_b64: str
    args: dict[str, Any] = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list)
    entrypoint: str = "run"
    timeout_s: float = Field(default=120.0, gt=0, le=600)
    install: bool = True


class PyRunResult(BaseModel):
    ok: bool
    result: Any | None = None
    error: str | None = None
    error_type: str | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    installed_path: str | None = None


class ToolTestParams(BaseModel):
    tool_name: str
    version: int
    sha256: str
    source_b64: str
    permissions: list[str] = Field(default_factory=list)
    entrypoint: str = "run"
    tests: list[ToolTestCase] = Field(default_factory=list)
    timeout_s: float = Field(default=120.0, gt=0, le=600)


class TestCaseResult(BaseModel):
    name: str
    ok: bool
    message: str = ""
    duration_ms: int = 0
    actual: Any | None = None
    stdout: str = ""
    stderr: str = ""


class ToolTestResult(BaseModel):
    passed: int = 0
    failed: int = 0
    results: list[TestCaseResult] = Field(default_factory=list)
    ok: bool = False


class ResetParams(BaseModel):
    keep_tools: bool = True


class SandboxMetrics(BaseModel):
    vm_id: str
    uptime_s: float
    commands_run: int
    bytes_written: int
    tool_runs: int
    workspace_bytes: int
    root_readonly: bool
    uid: int
    network_interfaces: list[str] = Field(default_factory=list)
    cgroup_available: bool = False


class GuestError(BaseModel):
    code: int
    message: str
    data: dict[str, Any] | None = None


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
