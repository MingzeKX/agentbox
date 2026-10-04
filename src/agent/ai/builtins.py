"""Built-in host-side tools that are not about the network or the sandbox.

Right now that is the clock.  It sounds trivial, but the operator asked for it
explicitly: the sandbox has no NIC and no NTP, so an agent that needs "what time is it"
had no way to find out.  This answers from the AI service (whose clock the host keeps
in sync) and costs nothing.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

BuiltinHandler = Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]


async def time_now(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001 - handler signature
    """Current time, in UTC and in the service's local zone.

    ``tz_offset_hours`` shifts the reported time when the caller wants another zone
    (the sandbox inherits the host clock, but its zone is UTC).
    """
    now = dt.datetime.now(dt.UTC)
    offset = args.get("tz_offset_hours")
    shifted = now
    if isinstance(offset, (int, float)) and not isinstance(offset, bool):
        if not -14 <= float(offset) <= 14:
            return {"ok": False, "error": "tz_offset_hours must be between -14 and 14", "error_code": "invalid_args"}
        shifted = now.astimezone(dt.timezone(dt.timedelta(hours=float(offset))))
    local = dt.datetime.now().astimezone()
    return {
        "ok": True,
        "utc": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "requested": shifted.isoformat(timespec="seconds"),
        "epoch": int(now.timestamp()),
        "service_local": local.isoformat(timespec="seconds"),
        "service_timezone": str(local.tzinfo),
        "weekday_utc": now.strftime("%A"),
        "source": "AI service clock (the sandbox has no NTP; this is the host-synced time)",
    }


BUILTIN_HANDLERS: dict[str, BuiltinHandler] = {
    "time.now": time_now,
}
