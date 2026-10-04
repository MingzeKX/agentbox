"""Permission tiers + the host.exec tool.

Tiers are a *policy ceiling* enforced in the AI service, independent of the individual
switches:

  safe          sandbox tools only (fs.*, exec.run, sandbox.*, toolsmith.*)
  trusted       + firewalled net.* tools, mcp.* tools
  unrestricted  + host.exec (runs on the control-plane machine, needs a phrase)

The ceiling is checked twice: the AI service refuses to dispatch a tool above the
current tier, and the control plane independently refuses host.exec unless *its* tier is
unrestricted and the phrase matches.  search_tools hides what the current tier cannot
use, so the model is not tempted.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.config import settings

log = logging.getLogger(__name__)

TIER_ORDER = {"safe": 0, "trusted": 1, "unrestricted": 2}

#: tool-name prefix -> minimum tier
PREFIX_TIERS: dict[str, str] = {
    "net.": "trusted",
    "mcp.": "trusted",
    "host.": "unrestricted",
}


class TierDenied(Exception):
    def __init__(self, tool: str, required: str, current: str) -> None:
        super().__init__(
            f"{tool} needs the {required!r} permission tier, current tier is {current!r}. "
            f"Raise it in the console: "
            + (
                f"/perm {required}"
                if required != "unrestricted"
                else "/perm unrestricted <AGENT_HOST_EXEC_PHRASE>   (runs commands on this machine)"
            )
        )
        self.tool = tool
        self.required = required
        self.current = current


def current_tier() -> str:
    return getattr(settings, "permission_tier", "safe")


def required_tier(tool: str) -> str:
    for prefix, tier in PREFIX_TIERS.items():
        if tool.startswith(prefix):
            return tier
    return "safe"


def allowed(tool: str, tier: str | None = None) -> bool:
    tier = tier or current_tier()
    return TIER_ORDER.get(tier, 0) >= TIER_ORDER[required_tier(tool)]


def check(tool: str, tier: str | None = None) -> None:
    """Raise TierDenied when ``tool`` is above the ceiling."""
    tier = tier or current_tier()
    needed = required_tier(tool)
    if TIER_ORDER.get(tier, 0) < TIER_ORDER[needed]:
        raise TierDenied(tool, needed, tier)


def visible(tool: str, tier: str | None = None) -> bool:
    """Whether search_tools may show this tool at the current tier."""
    return allowed(tool, tier)


def describe() -> dict[str, Any]:
    tier = current_tier()
    return {
        "tier": tier,
        "tools": {
            name: {"required": required, "available": TIER_ORDER.get(tier, 0) >= TIER_ORDER[required]}
            for name, required in (
                ("fs.*, exec.run, sandbox.*", "safe"),
                ("toolsmith.*", "safe"),
                ("net.*", "trusted"),
                ("mcp.*", "trusted"),
                ("host.exec", "unrestricted"),
            )
        },
    }
