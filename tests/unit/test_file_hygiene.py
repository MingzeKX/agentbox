"""File hygiene that only matters when a Windows checkout is shipped to Linux.

Three failure modes were hit for real while bringing this project up:

* ``install-platform.sh`` arrived with CRLF, so bash read ``set -euo pipefail`` as
  ``pipefail\\r`` and aborted with "invalid option name".
* a preseed file with CRLF silently gives every debconf value a trailing ``\\r``.
* a systemd unit with CRLF makes systemd reject directives.
* a ``.ps1`` without a UTF-8 BOM is decoded as GBK by Windows PowerShell 5.1,
  which mangles every Chinese string in it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKIP = {".git", ".venv", "qemu", "var", "__pycache__", ".pytest_cache", ".ruff_cache"}


def files(*suffixes: str):
    out = []
    for path in sorted(REPO.rglob("*")):
        if not path.is_file() or any(part in SKIP for part in path.parts):
            continue
        if path.suffix.lower() in suffixes:
            out.append(path)
    return out


def rel(path: Path) -> str:
    return str(path.relative_to(REPO)).replace("\\", "/")


@pytest.mark.parametrize("path", files(".sh", ".tpl", ".service"), ids=rel)
def test_linux_consumed_files_use_lf(path: Path):
    raw = path.read_bytes()
    assert b"\r\n" not in raw, (
        f"{rel(path)} has CRLF line endings. bash/systemd/the installer will misparse it "
        "(e.g. `set -o pipefail\\r`); run `python var/normalize_lf.py` or fix the editor."
    )


@pytest.mark.parametrize("path", files(".sh", ".tpl"), ids=rel)
def test_linux_consumed_files_have_no_bom(path: Path):
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), (
        f"{rel(path)} starts with a UTF-8 BOM; debconf/bash do not strip it and "
        "cloud-init rejects a user-data file whose first bytes are not '#cloud-config'."
    )


@pytest.mark.parametrize("path", files(".ps1"), ids=rel)
def test_powershell_scripts_carry_a_bom(path: Path):
    """Windows PowerShell 5.1 decodes BOM-less files as the ANSI codepage."""
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), (
        f"{rel(path)} has no UTF-8 BOM, so PowerShell 5.1 will mangle its non-ASCII text"
    )


@pytest.mark.parametrize("path", files(".py", ".sh", ".ps1", ".tpl"), ids=rel)
def test_no_encoding_replacement_characters(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    assert "\ufffd" not in text, (
        f"{rel(path)} contains U+FFFD: it was written by a tool that decoded UTF-8 as GBK. "
        "Rewrite the affected strings (tests/check_encoding.py lists every such file)."
    )


def test_the_hygiene_checks_actually_look_at_something():
    assert len(files(".sh")) >= 2, "no shell scripts were found"
    assert len(files(".ps1")) >= 5, "no PowerShell scripts were found"
    assert len(files(".service")) >= 2, "no systemd units were found"
