"""Entry point for ``python -m agent.cli <subcommand>``.

Without this file ``agent.cli`` is a package with no ``__main__`` and
``python -m agent.cli ...`` fails with
"No module named agent.cli.__main__", which is how every document, deploy script
and systemd unit invokes the CLI.
"""

from __future__ import annotations

import sys

from agent.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
