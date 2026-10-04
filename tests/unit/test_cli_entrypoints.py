"""The documented CLI entry points must actually exist and run.

Every document, deploy script and systemd unit invokes the CLI as
``python -m agent.cli <subcommand>``; if ``agent/cli/__main__.py`` is missing that
fails with "No module named agent.cli.__main__", so this is regression tested by
really executing the module in a subprocess.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from agent.cli.main import build_parser, main


def test_dunder_main_module_exists():
    import agent.cli.__main__ as entry  # noqa: PLC0415

    assert callable(entry.main)


def test_module_entry_point_prints_help():
    result = subprocess.run(
        [sys.executable, "-m", "agent.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "serve" in result.stdout
    assert "sandbox" in result.stdout
    assert "doctor" in result.stdout


@pytest.mark.parametrize("role", ["ai", "control"])
def test_serve_subcommands_are_wired(role: str):
    args = build_parser().parse_args(["serve", role])
    assert args.role == role
    assert callable(args.func)


def test_main_returns_an_int_for_bad_args(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["agent"])
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2  # argparse usage error


def test_documented_invocations_parse():
    """Every command line printed in README.md / the deploy scripts must parse."""
    documented = [
        ["db", "init"],
        ["db", "seed"],
        ["db", "ping"],
        ["image", "verify"],
        ["image", "build"],
        ["doctor"],
        ["chat"],
        ["chat", "-m", "hi"],
        ["tools", "list", "-q", "读取文件"],
        ["tools", "show", "fs.read", "--source"],
        ["tools", "runs", "fs.read"],
        ["tools", "check", "manifest.json"],
        ["sandbox", "status"],
        ["sandbox", "start", "smoke"],
        ["sandbox", "reset", "s1"],
        ["sandbox", "console", "vm-123"],
        ["sandbox", "invoke", "sandbox.info", "--session", "s1"],
        ["serve", "ai", "--port", "8090"],
        ["serve", "control", "--port", "8091"],
    ]
    for argv in documented:
        build_parser().parse_args(argv)
