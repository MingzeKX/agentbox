"""Personas: the operator's way to steer tone and role without touching policy.

A persona is a markdown file injected into the system prompt as a *style* section.
It cannot change what the agent is allowed to do -- every permission lives in code
(tool tiers, the network firewall, the sandbox), never in the prompt.  The safety
sections of the base prompt are appended *after* the persona, so a persona cannot
talk the agent out of them.

Resolution order for a name (sanitised, no separators): the operator directory
(``AGENT_PERSONA_DIR``, default ``<repo>/personas``) first -- so a deployment can
override a bundled persona -- then the built-in package directory.  Names are
checked against a strict pattern, so a model- or user-supplied string can never walk
out of those directories.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from agent.config import settings

log = logging.getLogger(__name__)

PERSONA_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
BUNDLED_DIR = Path(__file__).parent / "personas"
MAX_PERSONA_CHARS = 4000

#: safe fallback when nothing is configured
DEFAULT_PERSONA = "engineer"


@dataclass(frozen=True)
class Persona:
    name: str
    text: str
    source: str  # "operator" | "bundled" | "default"


def operator_dir() -> Path:
    configured = settings.persona_dir
    if configured:
        return Path(configured)
    return settings.var_dir.parent / "personas"


def _read(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return text[:MAX_PERSONA_CHARS]


def available() -> dict[str, str]:
    """Map persona name -> source, operator files winning over bundled ones."""
    found: dict[str, str] = {}
    for directory, source in ((BUNDLED_DIR, "bundled"), (operator_dir(), "operator")):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            name = path.stem
            if PERSONA_RE.match(name):
                found[name] = source
    return found


def names() -> list[str]:
    return sorted(available())


def load(name: str | None = None) -> Persona:
    """Load a persona by name; unknown or invalid names fall back to the default."""
    wanted = (name or settings.persona or DEFAULT_PERSONA).strip().lower()
    if not PERSONA_RE.match(wanted):
        log.warning("ignoring invalid persona name %r", name)
        wanted = DEFAULT_PERSONA
    for directory, source in ((operator_dir(), "operator"), (BUNDLED_DIR, "bundled")):
        candidate = directory / f"{wanted}.md"
        if candidate.is_file():
            try:
                return Persona(name=wanted, text=_read(candidate).strip(), source=source)
            except OSError as exc:  # pragma: no cover - unreadable file
                log.warning("cannot read persona %s: %s", candidate, exc)
    if wanted != DEFAULT_PERSONA:
        log.warning("persona %r not found (available: %s)", wanted, ", ".join(names()) or "none")
    bundled = BUNDLED_DIR / f"{DEFAULT_PERSONA}.md"
    text = _read(bundled).strip() if bundled.is_file() else ""
    return Persona(name=DEFAULT_PERSONA, text=text, source="default")


def build_system_prompt(base: str, persona: Persona, facts: list[str]) -> str:
    """Assemble base prompt + persona + runtime facts.

    Order matters: the persona goes after the base prompt's contract but before the
    runtime section, and the base prompt's safety text is never removed.
    """
    parts = [base.rstrip()]
    if persona.text:
        parts.append(f"# Persona: {persona.name}\n\n{persona.text}")
    if facts:
        parts.append("# Runtime\n\n" + "\n".join(f"* {line}" for line in facts))
    return "\n\n".join(parts) + "\n"
