"""Find every text file whose non-ASCII characters were mangled (U+FFFD).

Cause: Windows PowerShell 5.1's Get-Content/Set-Content pair decodes BOM-less
UTF-8 as GBK, so any earlier in-place edit of a UTF-8-without-BOM file that
contained Chinese destroyed those characters.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "qemu", "var", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
SUFFIXES = {".py", ".ps1", ".sh", ".md", ".cfg", ".tpl", ".toml", ".example", ".json", ".txt", ".service", ".gitignore"}

rows: list[tuple[str, int, int]] = []
for path in sorted(REPO.rglob("*")):
    if not path.is_file():
        continue
    if any(part in SKIP_DIRS for part in path.parts):
        continue
    if path.suffix.lower() not in SUFFIXES and path.name not in {".gitignore", ".env.example"}:
        continue
    if path.stat().st_size > 4_000_000:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        rows.append((str(path.relative_to(REPO)), -1, -1))
        continue
    bad = text.count("\ufffd")
    if bad:
        rows.append((str(path.relative_to(REPO)), bad, len(text)))

if not rows:
    print("no mangled files found: every text file decodes cleanly")
    sys.exit(0)

print(f"{'file':52s} {'U+FFFD':>7s} {'chars':>8s}")
for name, bad, total in rows:
    print(f"{name:52s} {('not-utf8' if bad < 0 else bad):>7} {total:>8}")
print(f"\n{len(rows)} file(s) affected")
sys.exit(1)
