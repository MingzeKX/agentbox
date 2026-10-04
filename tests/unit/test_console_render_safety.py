"""A rich Console in a test must never target the real terminal.

Live failure: two tests built ``Console(record=True, width=N, no_color=True)`` and the
legacy Windows renderer blocked forever writing to a redirected stdout -- the whole
suite produced no output and pytest-timeout could not kill the blocked thread.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
BAD = re.compile(r"Console\((?![^)]*force_terminal=False)[^)]*record=True[^)]*\)")


def test_rich_consoles_are_not_terminal_bound():
    offenders: list[str] = []
    for path in sorted(TESTS.glob("test_*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if BAD.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "rich Console without force_terminal=False:\n" + "\n".join(offenders)
