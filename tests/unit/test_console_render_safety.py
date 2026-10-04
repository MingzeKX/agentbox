"""A rich Console in a test must never target the real terminal.

Live failure: several tests built a recording Console without ``force_terminal=False`` and
the legacy Windows renderer blocked forever writing to a redirected stdout -- the whole
suite produced no output and pytest-timeout could not kill the blocked thread.

The scan is done on the *parsed* module, not on its text: this docstring has to be able to
name the broken shape, a helper class called ``FakeConsole`` is not a Console call, and a
construction wrapped over several lines still counts (a line-by-line grep misses those).
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _recording_consoles(source: str, filename: str) -> list[tuple[int, str]]:
    """Every ``Console(record=True)`` call that does not pin ``force_terminal=False``."""
    lines = source.splitlines()
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "Console":
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        record = keywords.get("record")
        if not (isinstance(record, ast.Constant) and record.value is True):
            continue
        force_terminal = keywords.get("force_terminal")
        if isinstance(force_terminal, ast.Constant) and force_terminal.value is False:
            continue
        text = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
        found.append((node.lineno, text))
    return found


def test_rich_consoles_are_not_terminal_bound():
    offenders: list[str] = []
    for path in sorted(TESTS.glob("test_*.py")):
        for number, text in _recording_consoles(path.read_text(encoding="utf-8"), str(path)):
            offenders.append(f"{path.name}:{number}: {text}")
    assert not offenders, "rich Console without force_terminal=False:\n" + "\n".join(offenders)


def test_the_scanner_catches_a_terminal_bound_console():
    """The hygiene check must fail on the shape it exists for: prove it on a sample."""
    sample = (
        "from rich.console import Console\n"
        "a = Console(record=True, width=110, no_color=True)\n"
        "b = Console(record=True, width=110, no_color=True, force_terminal=False)\n"
        "c = FakeConsole(record=True, width=110)\n"
    )
    assert [number for number, _ in _recording_consoles(sample, "sample.py")] == [2]
