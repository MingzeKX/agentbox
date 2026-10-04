"""AST based static checker for model authored tools (standard library only).

The checker is intentionally an **allow-list**: a tool may import a small set of
pure/offline standard library modules and nothing else.  It never executes the
code it inspects -- it only parses it -- so it is safe to run on the host and is
also run inside the sandbox before any test case is executed.

Contract enforced for a tool module:

* module level ``def <entrypoint>(args)`` with exactly one positional parameter
* imports limited to :data:`ALLOWED_IMPORTS`
* no dynamic evaluation, no ``open()``, no dunder attribute access, no dynamic
  attribute access helpers (``getattr``/``setattr``/``delattr``)
* file and command access only through the injected ``fs`` / ``sh`` helpers, and
  only for the permissions declared in the manifest
* no ``while True`` without a ``break`` (bounded by the execution timeout anyway)
* bounded size: 16 KiB / 400 lines / 4000 AST nodes

Each finding is a structured violation with a rule id, a line number and a
``severity``; only ``error`` severity blocks registration.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

MAX_SOURCE_BYTES = 16_384
MAX_LINES = 400
MAX_NODES = 4_000
MAX_FUNCTIONS = 40

#: root module -> allowed sub-modules (``None`` means "the whole module")
ALLOWED_IMPORTS: dict[str, set[str] | None] = {
    "__future__": None,
    "abc": None,
    "base64": None,
    "binascii": None,
    "bisect": None,
    "calendar": None,
    "collections": None,
    "contextlib": None,
    "copy": None,
    "csv": None,
    "dataclasses": None,
    "datetime": None,
    "decimal": None,
    "difflib": None,
    "enum": None,
    "fractions": None,
    "functools": None,
    "gzip": None,
    "hashlib": None,
    "heapq": None,
    "html": None,
    "io": None,  # dangerous attributes are denied below
    "itertools": None,
    "json": None,
    "math": None,
    "numbers": None,
    "operator": None,
    "pprint": None,
    "random": None,
    "re": None,
    "secrets": None,
    "statistics": None,
    "string": None,
    "struct": None,
    "textwrap": None,
    "typing": None,
    "unicodedata": None,
    "urllib": {"parse"},  # urllib.request / urllib.error are NOT allowed
    "uuid": None,
    "zlib": None,
    "zoneinfo": None,
}

#: module.attribute pairs that are denied even though the module is allowed
DENIED_ATTRIBUTES: dict[str, set[str]] = {
    "io": {
        "open",
        "open_code",
        "FileIO",
        "TextIOWrapper",
        "BufferedReader",
        "BufferedWriter",
        "BufferedRandom",
        "BufferedRWPair",
    },
    "gzip": {"open", "GzipFile"},
    "operator": {"attrgetter", "methodcaller"},
    "typing": {"get_type_hints"},
}

#: names that may legitimately be referenced even though they look like dunders
#: modules added by the "extended" profile: real scripting without the exotic corners
EXTENDED_IMPORTS: frozenset[str] = frozenset(
    {
        "os",
        "sys",
        "time",
        "pathlib",
        "glob",
        "shutil",
        "tempfile",
        "subprocess",
        "smtplib",
        "email",
        "sqlite3",
        "zipfile",
        "tarfile",
        "logging",
        "traceback",
        "threading",
        "queue",
        "concurrent",
        "socket",
        "select",
        "platform",
        "stat",
        "errno",
        "signal",
        "getpass",
        "pwd",
        "grp",
    }
)

#: profile name -> extra root modules (None = only ALLOWED_IMPORTS, "*" = anything)
PROFILES: dict[str, frozenset[str] | None | str] = {
    "strict": None,
    "extended": EXTENDED_IMPORTS,
    "unrestricted": "*",
}

#: modules that only make sense with a permission, and the permission they need
#: appended to an import rejection so the fix is one copy-paste away
HOW_TO_WIDEN = (
    "; to allow it: /config tool_extra_modules {module}   (only this module)  "
    "or /config tool_import_profile extended   (the usual scripting set)"
)


PERMISSION_GATED_MODULES: dict[str, str] = {
    "subprocess": "exec",
    "smtplib": "net",
    "socket": "net",
    "http": "net",
    "urllib": "net",
}


ALLOWED_DUNDER_NAMES = frozenset({"__name__", "__doc__"})

#: names that are never allowed to be referenced
#: names that a profile may re-enable when the tool declares the right permission
PERMISSION_GATED_NAMES: dict[str, str] = {
    "open": "fs.",
    "open_code": "fs.",
}


FORBIDDEN_NAMES = frozenset(
    {
        "__builtins__",
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "eval",
        "exec",
        "exit",
        "getattr",
        "globals",
        "input",
        "license",
        "locals",
        "memoryview",
        "open",
        "quit",
        "setattr",
        "vars",
    }
)

#: injected helper namespace -> attribute -> required permission
INJECTED_API: dict[str, dict[str, str | None]] = {
    "fs": {
        "read_text": "fs.read",
        "read_bytes": "fs.read",
        "read_json": "fs.read",
        "list_dir": "fs.read",
        "glob": "fs.read",
        "exists": "fs.read",
        "stat": "fs.read",
        "write_text": "fs.write",
        "write_bytes": "fs.write",
        "write_json": "fs.write",
        "append_text": "fs.write",
        "mkdir": "fs.write",
        "remove": "fs.write",
        "move": "fs.write",
        "workspace": None,
    },
    "sh": {
        "run": "exec",
        "capture": "exec",
        "which": "exec",
        "env": "exec",
    },
}

TOP_LEVEL_OK = (
    ast.Import,
    ast.ImportFrom,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Assign,
    ast.AnnAssign,
    ast.If,
    ast.Try,
)


@dataclass
class Finding:
    rule: str
    message: str
    line: int | None = None
    col: int | None = None
    severity: str = "error"

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "message": self.message,
            "line": self.line,
            "col": self.col,
            "severity": self.severity,
        }


@dataclass
class Report:
    ok: bool
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "violations": [f.as_dict() for f in self.findings], "stats": self.stats}


class _Visitor(ast.NodeVisitor):
    def __init__(self, entrypoint: str, permissions: Iterable[str]) -> None:
        self.entrypoint = entrypoint
        self.permissions = set(permissions)
        self.findings: list[Finding] = []
        self.imports: list[str] = []
        self.functions: list[str] = []
        self.nodes = 0
        self.max_nesting = 0
        self.used_permissions: set[str] = set()
        self.entrypoint_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None
        self._nesting = 0

    # ------------------------------------------------------------- utilities
    def report(self, rule: str, message: str, node: ast.AST | None = None, severity: str = "error") -> None:
        self.findings.append(
            Finding(
                rule=rule,
                message=message,
                line=getattr(node, "lineno", None),
                col=getattr(node, "col_offset", None),
                severity=severity,
            )
        )

    # --------------------------------------------------------------- walking
    def visit(self, node: ast.AST) -> Any:  # noqa: D102
        self.nodes += 1
        return super().visit(node)

    def _nested(self, node: ast.AST) -> None:
        self._nesting += 1
        self.max_nesting = max(self.max_nesting, self._nesting)
        self.generic_visit(node)
        self._nesting -= 1

    # --------------------------------------------------------------- imports
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_module(alias.name, node, alias.asname)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level and node.level > 0:
            self.report("relative_import", "relative imports are not allowed", node)
            return
        module = node.module or ""
        if node.names and any(alias.name == "*" for alias in node.names):
            self.report("star_import", "'from ... import *' is not allowed", node)
            return
        root, _, sub = module.partition(".")
        allowed = ALLOWED_IMPORTS.get(root, "missing")
        if allowed == "missing":
            self.report("import_forbidden", f"module {root!r} is not in the allow-list", node)
            return
        if allowed is not None and sub and sub not in allowed:
            self.report("import_forbidden", f"module {module!r} is not in the allow-list", node)
            return
        if allowed is not None and not sub:
            for alias in node.names:
                if alias.name not in allowed:
                    self.report(
                        "import_forbidden",
                        f"'from {root} import {alias.name}' is not allowed; only {sorted(allowed)} may be imported",
                        node,
                    )
        self.imports.append(module or root)
        self.generic_visit(node)

    def _check_module(self, name: str, node: ast.AST, asname: str | None) -> None:
        root, _, sub = name.partition(".")
        allowed = ALLOWED_IMPORTS.get(root, "missing")
        if allowed == "missing":
            self.report("import_forbidden", f"module {root!r} is not in the allow-list", node)
            return
        if allowed is not None and sub and sub not in allowed:
            self.report("import_forbidden", f"module {name!r} is not in the allow-list", node)
            return
        self.imports.append(name)

    # ----------------------------------------------------------------- names
    def visit_Name(self, node: ast.Name) -> None:
        name = node.id
        if name in ALLOWED_DUNDER_NAMES:
            self.generic_visit(node)
            return
        if name in FORBIDDEN_NAMES or (name.startswith("__") and name.endswith("__")):
            self.report("forbidden_name", f"use of {name!r} is not allowed", node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        attr = node.attr
        if attr.startswith("__") and attr.endswith("__"):
            self.report("dunder_attribute", f"access to dunder attribute {attr!r} is not allowed", node)
        if isinstance(node.value, ast.Name):
            base = node.value.id
            if base in DENIED_ATTRIBUTES and attr in DENIED_ATTRIBUTES[base]:
                self.report("forbidden_attribute", f"{base}.{attr} is not allowed", node)
            if base in INJECTED_API:
                if attr not in INJECTED_API[base]:
                    self.report(
                        "unknown_api",
                        f"unknown sandbox helper {base}.{attr}; allowed: {sorted(INJECTED_API[base])}",
                        node,
                    )
                else:
                    required = INJECTED_API[base][attr]
                    if required:
                        self.used_permissions.add(required)
                        if required not in self.permissions:
                            self.report(
                                "permission_undeclared",
                                f"{base}.{attr} requires the {required!r} permission; add it to permissions",
                                node,
                            )
                    if base == "sh" and attr in {"run", "capture"} and "exec.shell" not in self.permissions:
                        keywords = getattr(node, "call_keywords", ())
                        shell_true = any(
                            kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                            for kw in keywords
                        )
                        if shell_true:
                            self.report(
                                "permission_undeclared",
                                "shell=True requires the 'exec.shell' permission",
                                node,
                            )
        self.generic_visit(node)

    # ------------------------------------------------------------ functions
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        if node.name == self.entrypoint:
            if self.entrypoint_node is not None:
                self.report("duplicate_entrypoint", f"{self.entrypoint}() is defined more than once", node)
            self.entrypoint_node = node
            a = node.args
            positional = list(a.posonlyargs) + list(a.args)
            if len(positional) != 1 or a.vararg or a.kwarg or a.kwonlyargs or len(a.defaults) > 0:
                self.report(
                    "entrypoint_signature",
                    f"{self.entrypoint}() must take exactly one positional argument named 'args' "
                    f"(no defaults, *args or **kwargs)",
                    node,
                )
            elif positional[0].arg != "args":
                self.report(
                    "entrypoint_signature",
                    f"{self.entrypoint}() argument must be named 'args', found {positional[0].arg!r}",
                    node,
                )
        if len(self.functions) > MAX_FUNCTIONS:
            self.report("too_many_functions", f"at most {MAX_FUNCTIONS} functions are allowed", node)
        self._nested(node)

    # ---------------------------------------------------------------- loops
    def visit_While(self, node: ast.While) -> None:
        if isinstance(node.test, ast.Constant) and node.test.value is True:
            if not any(isinstance(child, ast.Break) for child in ast.walk(node)):
                self.report("unbounded_loop", "'while True' without a break is not allowed", node)
        self._nested(node)

    # ------------------------------------------------------------- structure
    def check_top_level(self, tree: ast.Module) -> None:
        for stmt in tree.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue  # module docstring
            if isinstance(stmt, TOP_LEVEL_OK):
                continue
            self.report(
                "top_level_statement",
                f"top level {type(stmt).__name__} is not allowed; keep the module to imports and definitions",
                stmt,
                severity="warning",
            )


def _bind_keywords(tree: ast.AST) -> None:
    """Attach the keyword list of the enclosing Call to each called attribute.

    A tiny pre-pass so the checker can answer "was shell=True passed here?" while
    staying a pure ``ast`` consumer.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            node.func.call_keywords = node.keywords  # type: ignore[attr-defined]


_IMPORT_RE = re.compile(r"module '([^']+)' is not in the allow-list")
_NAME_RE = re.compile(r"use of '([^']+)' is not allowed")


def _apply_policy(
    findings: Iterable[Finding],
    *,
    profile: str,
    extra_modules: Iterable[str],
    allow_open: bool,
    permissions: Iterable[str],
) -> list[Finding]:
    """Re-judge the visitor's import/name findings against the operator's profile.

    The AST visitor always reports against the strict allow-list; this keeps that code
    untouched and makes the policy a single, reviewable decision point.  A module that
    is merely *permission gated* gets a precise "declare permission X" finding instead
    of a vague "not in the allow-list", which is what the model needs to fix itself.
    """
    allowed = allowed_modules(profile, extra_modules)
    names = forbidden_names(allow_open, permissions)
    kept: list[Finding] = []
    for finding in findings:
        if finding.rule == "import_forbidden":
            match = _IMPORT_RE.search(finding.message)
            module = match.group(1) if match else ""
            root, _, sub = module.partition(".")
            sub = sub or None
            # does the profile allow this exact module (root *and* sub-module)?
            if allowed is None:
                profile_allows = True
            elif root not in allowed:
                profile_allows = False
            else:
                subs = allowed[root]
                profile_allows = subs is None or (sub is not None and sub in subs) or (sub is None and not subs)
            if not profile_allows:
                kept.append(
                    Finding(
                        finding.rule,
                        finding.message + HOW_TO_WIDEN.format(module=root),
                        line=finding.line,
                        col=finding.col,
                        severity=finding.severity,
                    )
                )
                continue
            needed = _module_gate(root, permissions)
            if needed:
                kept.append(
                    Finding(
                        "permission_missing",
                        f"module {root!r} needs the {needed!r} permission; "
                        f"declare it in the manifest (or drop the import)",
                        line=finding.line,
                        col=finding.col,
                    )
                )
                continue
            continue  # allowed by the profile and, if gated, permitted
        if finding.rule == "forbidden_name":
            match = _NAME_RE.search(finding.message)
            name = match.group(1) if match else ""
            if name in names or (allow_open and name in PERMISSION_GATED_NAMES and name not in FORBIDDEN_NAMES):
                kept.append(finding)  # still forbidden
                continue
            if name not in names:  # re-enabled by the profile + a declared permission
                continue
            kept.append(finding)
            continue
        kept.append(finding)
    return kept


def allowed_modules(profile: str, extra: Iterable[str] = ()) -> dict[str, set[str] | None] | None:
    """Root module -> allowed sub-modules (``None`` = whole module), or None for "any".

    Unknown profile names fall back to ``strict`` -- a typo must never widen the surface.
    """
    name = (profile or "strict").strip().lower()
    if name not in PROFILES:
        name = "strict"
    if PROFILES[name] == "*":
        return None
    allowed: dict[str, set[str] | None] = {root: subs for root, subs in ALLOWED_IMPORTS.items()}
    extra_profile = PROFILES[name]
    if extra_profile:
        for module in extra_profile:
            allowed[module] = None  # whole module, not just the sub-modules strict allows
    for module in extra:
        module = str(module).strip()
        if module:
            allowed[module.split(".")[0]] = None
    return allowed


def forbidden_names(allow_open: bool, permissions: Iterable[str]) -> frozenset[str]:
    """The never-allowed name set, minus what a declared permission re-enables."""
    names = set(FORBIDDEN_NAMES)
    if not allow_open:
        return frozenset(names)
    declared = set(permissions)
    for name, needed in PERMISSION_GATED_NAMES.items():
        if needed.endswith("."):  # prefix permission such as "fs."
            if any(permission.startswith(needed) for permission in declared):
                names.discard(name)
        elif needed in declared:
            names.discard(name)
    return frozenset(names)


def _module_gate(module: str, permissions: Iterable[str]) -> str | None:
    """Permission a module needs, or None when it needs none."""
    needed = PERMISSION_GATED_MODULES.get(module)
    if needed is None:
        return None
    return None if needed in set(permissions) else needed


def check_source(
    source: str,
    permissions: Iterable[str] = (),
    entrypoint: str = "run",
    *,
    profile: str = "strict",
    extra_modules: Iterable[str] = (),
    allow_open: bool = False,
) -> Report:
    """Parse and vet ``source``.  Never executes it."""
    findings: list[Finding] = []
    encoded = source.encode("utf-8")
    if len(encoded) > MAX_SOURCE_BYTES:
        findings.append(
            Finding("source_too_large", f"source is {len(encoded)} bytes, limit is {MAX_SOURCE_BYTES}")
        )
        return Report(ok=False, findings=findings, stats={})
    lines = source.count("\n") + 1
    if lines > MAX_LINES:
        findings.append(Finding("source_too_long", f"source has {lines} lines, limit is {MAX_LINES}"))
        return Report(ok=False, findings=findings, stats={})

    try:
        tree = ast.parse(source, filename="<tool>", mode="exec")
    except SyntaxError as exc:
        findings.append(
            Finding("syntax_error", f"{exc.msg} (line {exc.lineno})", line=exc.lineno, col=exc.offset)
        )
        return Report(ok=False, findings=findings, stats={})

    _bind_keywords(tree)
    visitor = _Visitor(entrypoint, permissions)
    visitor.check_top_level(tree)
    visitor.visit(tree)

    if visitor.entrypoint_node is None:
        findings.append(
            Finding("missing_entrypoint", f"module must define a top level function {entrypoint}(args)")
        )

    if visitor.nodes > MAX_NODES:
        findings.append(
            Finding("too_complex", f"module has {visitor.nodes} AST nodes, limit is {MAX_NODES}")
        )

    unused = visitor.permissions - visitor.used_permissions - {"exec.shell"}
    if unused:
        findings.append(
            Finding(
                "permission_unused",
                f"declared permission(s) never used: {', '.join(sorted(unused))}",
                severity="warning",
            )
        )

    findings.extend(
        _apply_policy(
            visitor.findings,
            profile=profile,
            extra_modules=extra_modules,
            allow_open=allow_open,
            permissions=permissions,
        )
    )
    ok = not any(f.severity == "error" for f in findings)
    stats = {
        "loc": lines,
        "imports": sorted(set(visitor.imports)),
        "functions": visitor.functions,
        "has_entrypoint": visitor.entrypoint_node is not None,
        "max_nesting": visitor.max_nesting,
        "nodes": visitor.nodes,
        "used_permissions": sorted(visitor.used_permissions),
    }
    return Report(ok=ok, findings=findings, stats=stats)


def violation_dicts(report: Report) -> list[dict[str, Any]]:
    return [f.as_dict() for f in report.findings]


#: kept for readability in callers that only want the boolean
def is_acceptable(source: str, permissions: Iterable[str] = (), entrypoint: str = "run") -> bool:
    return check_source(source, permissions, entrypoint).ok


_WS = re.compile(r"\s+")


def one_line(source: str, limit: int = 120) -> str:
    return _WS.sub(" ", source).strip()[:limit]
