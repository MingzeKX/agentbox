"""Static analysis gate: the checker must reject every escape hatch we claim.

These tests are the security contract of the tool authoring pipeline -- if one of
them starts passing something it should not, the sandbox guarantee is void.
"""

from __future__ import annotations

import pytest

from agent.sandbox.checker import check_source

GOOD_SOURCE = '''
import json
import math


def run(args):
    """Square a list of numbers."""
    values = args.get("values") or []
    return {"squares": [v * v for v in values], "count": len(values)}
'''


def rules(source: str, permissions: list[str] | None = None) -> set[str]:
    report = check_source(source, permissions or [])
    return {finding.rule for finding in report.findings}


# --------------------------------------------------------------------------- #
# happy paths
# --------------------------------------------------------------------------- #


def test_accepts_a_clean_tool():
    report = check_source(GOOD_SOURCE)
    assert report.ok, [f.as_dict() for f in report.findings]
    assert report.stats["has_entrypoint"] is True
    assert report.stats["imports"] == ["json", "math"]


def test_accepts_injected_fs_with_declared_permission():
    source = """
def run(args):
    text = fs.read_text("/workspace/in.txt")
    fs.write_text("/workspace/out.txt", text.upper())
    return {"chars": len(text)}
"""
    report = check_source(source, ["fs.read", "fs.write"])
    assert report.ok, [f.as_dict() for f in report.findings]
    assert report.stats["used_permissions"] == ["fs.read", "fs.write"]


def test_accepts_shell_with_declared_permission():
    source = """
def run(args):
    out = sh.run(["echo", "hi"], shell=True)
    return {"exit": out["exit_code"]}
"""
    report = check_source(source, ["exec", "exec.shell"])
    assert report.ok, [f.as_dict() for f in report.findings]


def test_main_guard_is_allowed():
    source = GOOD_SOURCE + '\nif __name__ == "__main__":\n    run({})\n'
    assert check_source(source).ok


# --------------------------------------------------------------------------- #
# imports
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "module",
    ["os", "sys", "subprocess", "socket", "ctypes", "importlib", "shutil", "pathlib",
     "tempfile", "glob", "pickle", "marshal", "threading", "multiprocessing", "asyncio",
     "signal", "resource", "pty", "http", "urllib.request", "gc", "inspect", "types",
     "builtins", "codecs", "sqlite3", "zipfile", "tarfile", "xml"],
)
def test_rejects_dangerous_imports(module: str):
    source = f"import {module}\n\n\ndef run(args):\n    return {{}}\n"
    report = check_source(source)
    assert not report.ok
    assert "import_forbidden" in rules(source)


def test_rejects_star_import_and_relative_import():
    assert "star_import" in rules("from json import *\n\n\ndef run(args):\n    return {}\n")
    assert "relative_import" in rules("from . import helper\n\n\ndef run(args):\n    return {}\n")


def test_allows_safe_stdlib_subset():
    source = (
        "from datetime import datetime\nimport collections\nfrom urllib.parse import quote\n\n\n"
        "def run(args):\n    return {'q': quote('a b'), 't': str(datetime.now())}\n"
    )
    assert check_source(source).ok


# --------------------------------------------------------------------------- #
# dynamic evaluation / escape hatches
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "snippet",
    [
        "return eval('1+1')",
        "return exec('x=1')",
        "return compile('1', '<s>', 'eval')",
        "return __import__('os').getcwd()",
        "return open('/etc/passwd').read()",
        "return getattr(args, 'keys')()",
        "return globals()",
        "return locals()",
        "return vars(args)",
        "return breakpoint()",
        "return input()",
        "return object.__subclasses__()",
        "return args.__class__.__mro__",
        "return (lambda: 0).__globals__",
    ],
)
def test_rejects_dynamic_evaluation(snippet: str):
    source = f"def run(args):\n    {snippet}\n"
    report = check_source(source)
    assert not report.ok, f"{snippet!r} should have been rejected"
    assert {"forbidden_name", "dunder_attribute"} & rules(source)


def test_io_open_is_blocked_even_though_io_is_allowed():
    source = "import io\n\n\ndef run(args):\n    return io.open('/etc/shadow').read()\n"
    assert "forbidden_attribute" in rules(source)


def test_gzip_open_is_blocked_but_compress_is_allowed():
    assert "forbidden_attribute" in rules("import gzip\n\n\ndef run(args):\n    return gzip.open('/x')\n")
    assert check_source("import gzip\n\n\ndef run(args):\n    return gzip.compress(b'x')\n").ok


# --------------------------------------------------------------------------- #
# entry point contract
# --------------------------------------------------------------------------- #


def test_requires_the_entrypoint():
    assert "missing_entrypoint" in rules("import json\n")


@pytest.mark.parametrize(
    "signature",
    ["def run():", "def run(a, b):", "def run(*args):", "def run(args, **kwargs):", "def run(other):"],
)
def test_entrypoint_signature_must_be_exact(signature: str):
    source = f"{signature}\n    return {{}}\n"
    assert "entrypoint_signature" in rules(source)


def test_duplicate_entrypoint_is_rejected():
    source = "def run(args):\n    return {}\n\n\ndef run(args):\n    return {'again': True}\n"
    assert "duplicate_entrypoint" in rules(source)


# --------------------------------------------------------------------------- #
# permissions
# --------------------------------------------------------------------------- #


def test_undeclared_file_access_is_rejected():
    source = "def run(args):\n    return {'x': fs.read_text('/workspace/a')}\n"
    assert "permission_undeclared" in rules(source)


def test_undeclared_shell_is_rejected():
    source = "def run(args):\n    return sh.run(['ls'], shell=True)\n"
    declared = check_source(source, ["exec"])
    assert "permission_undeclared" in {f.rule for f in declared.findings}


def test_unknown_helper_is_rejected():
    source = "def run(args):\n    return fs.teleport('/workspace/a')\n"
    assert "unknown_api" in rules(source, ["fs.read"])


def test_unused_permission_is_only_a_warning():
    source = "def run(args):\n    return {'ok': True}\n"
    report = check_source(source, ["exec"])
    assert report.ok
    assert any(f.rule == "permission_unused" and f.severity == "warning" for f in report.findings)


# --------------------------------------------------------------------------- #
# structure / size
# --------------------------------------------------------------------------- #


def test_unbounded_loop_is_rejected():
    source = "def run(args):\n    n = 0\n    while True:\n        n += 1\n    return {'n': n}\n"
    assert "unbounded_loop" in rules(source)


def test_while_true_with_break_is_allowed():
    source = "def run(args):\n    while True:\n        break\n    return {'ok': True}\n"
    assert check_source(source).ok


def test_syntax_error_is_reported_not_raised():
    report = check_source("def run(args)\n    return {}\n")
    assert not report.ok
    assert report.findings[0].rule == "syntax_error"


def test_oversized_source_is_rejected():
    report = check_source("def run(args):\n    return {}\n" + "# padding\n" * 5000)
    assert not report.ok
    assert report.findings[0].rule == "source_too_large"


def test_too_many_lines_is_rejected():
    report = check_source("x = 1\n" * 500 + "def run(args):\n    return {}\n")
    assert not report.ok


def test_top_level_statement_is_a_warning_only():
    source = "import json\nprint('side effect')\n\n\ndef run(args):\n    return {}\n"
    report = check_source(source)
    assert report.ok
    assert any(f.rule == "top_level_statement" and f.severity == "warning" for f in report.findings)
