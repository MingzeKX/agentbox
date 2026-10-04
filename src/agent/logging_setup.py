"""Logging setup shared by every process (AI service, control plane, CLI, guest)."""

from __future__ import annotations

import logging
import sys

from agent.config import settings

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"


def setup_logging(level: str | None = None) -> None:
    root = logging.getLogger()
    if root.handlers:
        root.setLevel((level or settings.log_level).upper())
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%H:%M:%S"))
    root.addHandler(handler)
    root.setLevel((level or settings.log_level).upper())
    logging.getLogger("httpx").setLevel("WARNING")
    logging.getLogger("httpcore").setLevel("WARNING")
    logging.getLogger("uvicorn.access").setLevel("WARNING")
