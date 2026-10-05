"""Runtime configuration for the AI service.

The AI service must not write files, so "change a setting" cannot mean "edit .env".
Instead this module mutates the in-process settings object (the same object every
subsystem already reads), which takes effect on the next call -- no restart.

Two rules keep it safe:

* only keys on the whitelist can be touched (a typo cannot silently change behaviour)
* values are validated by the pydantic model itself (``validate_assignment``), so an
  invalid value is rejected instead of half-applied

Every applied change is logged as ``admin.config`` so the journal keeps a trail.
Persistence is deliberately out of scope: the CLI can write the same values into the
VM's ``.env`` (see ``/save``) when the operator wants them to survive a restart.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

from agent.ai import personas
from agent.config import settings

log = logging.getLogger(__name__)

#: key -> human hint, for /help and for error messages
MUTABLE: dict[str, str] = {
    "persona": "which persona styles the answers (see /persona)",
    "custom_prompt_file": "operator's own prompt file (absolute path, may be outside the repo)",
    "prompt_mode": "full | custom_only: custom_only loads only the operator's own file (+ runtime facts)",
    "net_enabled": "master switch for the firewalled net.* tools",
    "net_allow_hosts": "comma separated allowlist, *.example.com wildcards, * for all",
    "net_allow_ports": "comma separated ports the firewall lets through",
    "net_max_bytes": "per-request response cap",
    "net_allow_private_hosts": "danger: also allow loopback/private addresses",
    "permission_tier": "safe | trusted | unrestricted (policy ceiling)",
    "max_steps": "how many tool steps one turn may take",
    "exec_default_timeout_s": "default timeout for a sandbox command",
    "search_k": "how many tools search_tools returns by default",
    "llm_model": "which model answers; deepseek-flash can see images, deepseek-v4-pro is text only",
    "llm_effort": "low | high | max; empty = gateway default (weak/noisy effect on this gateway)",
    "llm_vision_model": "model used automatically for turns that carry an image",
    "tool_import_profile": "strict | extended | unrestricted: import allow-list for self-written tools",
    "tool_extra_modules": "comma separated extra modules allowed on top of the profile",
    "tool_allow_open": "allow open() in tools that declare fs.read/fs.write",
}

#: values accepted by the boolean-ish keys
#: how to cancel a persona from the console
PERSONA_ALIASES = {"off": "engineer", "none": "engineer", "default": "engineer", "neutral": "engineer"}

TRUEISH = {"1", "true", "yes", "on", "enable", "enabled"}
FALSEISH = {"0", "false", "no", "off", "disable", "disabled"}


class ConfigError(Exception):
    """A rejected change (unknown key, bad value, invalid persona)."""


def _coerce(key: str, value: Any) -> Any:
    field = type(settings).model_fields.get(key)
    if field is None:  # pragma: no cover - guarded by MUTABLE
        raise ConfigError(f"unknown setting {key!r}")
    annotation = field.annotation
    if annotation is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in TRUEISH:
            return True
        if text in FALSEISH:
            return False
        raise ConfigError(f"{key} expects a boolean, got {value!r}")
    if annotation is int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{key} expects an integer, got {value!r}") from exc
    if annotation is float:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{key} expects a number, got {value!r}") from exc
    return str(value)


def effective() -> dict[str, Any]:
    """Current values of every mutable key (for /net, /perm, GET /admin/config)."""
    return {key: getattr(settings, key) for key in MUTABLE}


def apply(changes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Apply ``{key: value}``; returns (effective values, list of "key: old -> new")."""
    if not isinstance(changes, dict) or not changes:
        raise ConfigError("nothing to set")
    unknown = sorted(set(changes) - set(MUTABLE))
    if unknown:
        raise ConfigError(f"cannot change {', '.join(unknown)}; mutable keys: {', '.join(sorted(MUTABLE))}")

    staged: dict[str, Any] = {}
    for key, raw in changes.items():
        value = _coerce(key, raw)
        if key == "persona":
            value = str(value).strip().lower()
            value = PERSONA_ALIASES.get(value, value)
            if value not in personas.available():
                raise ConfigError(
                    f"unknown persona {value!r}; available: {', '.join(personas.names()) or 'none'}"
                )
        staged[key] = value

    before = effective()
    changes_made: list[str] = []
    try:
        for key, value in staged.items():
            setattr(settings, key, value)
    except Exception as exc:  # noqa: BLE001 - pydantic validation error
        for key, value in before.items():  # roll back to the previous state
            with contextlib.suppress(Exception):
                setattr(settings, key, value)
        raise ConfigError(str(exc)) from exc

    for key, value in staged.items():
        if before[key] != value:
            changes_made.append(f"{key}: {before[key]!r} -> {value!r}")
    if changes_made:
        log.info("admin.config %s", json.dumps({"changes": changes_made}, ensure_ascii=False))
    return effective(), changes_made
