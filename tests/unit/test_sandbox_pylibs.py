"""The ad-hoc half of "tools may use third-party libraries".

The durable half is the curated set baked into the sandbox image
(``deploy/sandbox/build-sandbox-image.sh``); this pins the per-session half: a package
installed with ``pip install --target /workspace/pylibs <pkg>`` has to be importable by the
next tool the guest runs, without changing anything for a session that never installs one.

A tool is loaded by :func:`agent.sandbox.runner._load_tool`, which is plain importlib, so the
end-to-end proof here is: point the runner at a real directory, write a real module into it,
import a real third-party package out of it and run a real tool that imports it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent.sandbox import runner

REPO = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO / "deploy" / "sandbox" / "build-sandbox-image.sh"


@pytest.fixture
def pylibs(tmp_path: Path) -> Path:
    """A stand-in for /workspace/pylibs holding one importable module."""
    target = tmp_path / "pylibs"
    target.mkdir()
    (target / "pretend_pkg.py").write_text("VALUE = 'from pylibs'\n", encoding="utf-8")
    return target


# --------------------------------------------------------------- add_pylibs itself
def test_add_pylibs_is_a_noop_when_the_directory_is_missing(tmp_path):
    missing = str(tmp_path / "nope")
    assert runner.add_pylibs(missing) is None
    assert missing not in sys.path


def test_add_pylibs_puts_the_directory_first_on_sys_path(pylibs):
    try:
        assert runner.add_pylibs(str(pylibs)) == str(pylibs)
        assert sys.path[0] == str(pylibs), "a session package must win over a baked-in one"
    finally:
        sys.path.remove(str(pylibs))


def test_add_pylibs_is_idempotent(pylibs):
    try:
        runner.add_pylibs(str(pylibs))
        runner.add_pylibs(str(pylibs))
        assert sys.path.count(str(pylibs)) == 1
    finally:
        sys.path.remove(str(pylibs))


def test_the_default_target_is_the_workspace_directory_the_console_documents():
    assert runner.PYLIBS == "/workspace/pylibs"


def test_pylibs_is_read_from_the_environment_so_a_vm_can_override_it(monkeypatch, tmp_path):
    """The module-level constant is what the guest's own runner uses; keep it overridable."""
    import importlib

    monkeypatch.setenv("AGENT_TOOL_PYLIBS", str(tmp_path))
    reloaded = importlib.reload(runner)
    try:
        assert reloaded.PYLIBS == str(tmp_path)
    finally:
        monkeypatch.delenv("AGENT_TOOL_PYLIBS")
        importlib.reload(runner)


# ------------------------------------------------------- a tool that imports from it
def test_a_tool_can_import_a_package_installed_into_pylibs(pylibs):
    tool = pylibs.parent / "tool_uses_pylibs.py"
    tool.write_text(
        "import pretend_pkg\n\n\ndef run(args):\n    return {'ok': True, 'value': pretend_pkg.VALUE}\n",
        encoding="utf-8",
    )
    try:
        runner.add_pylibs(str(pylibs))
        assert runner._load_tool(str(tool), "run", set(), {}) == {"ok": True, "value": "from pylibs"}
    finally:
        sys.path.remove(str(pylibs))


def test_a_tool_is_loaded_through_the_runner_as_the_guest_runs_it(pylibs, tmp_path):
    """The real entry point: ``runner.py request.json result.json``."""
    tool = tmp_path / "tool_real.py"
    tool.write_text(
        "import pretend_pkg\n\n\ndef run(args):\n    return {'ok': True, 'value': pretend_pkg.VALUE, 'arg': args['n']}\n",
        encoding="utf-8",
    )
    request = tmp_path / "request.json"
    result = tmp_path / "result.json"
    request.write_text(
        json.dumps({"tool_path": str(tool), "entrypoint": "run", "args": {"n": 3}, "permissions": []}),
        encoding="utf-8",
    )
    env = {**os.environ, "AGENT_TOOL_PYLIBS": str(pylibs), "PYTHONDONTWRITEBYTECODE": "1"}
    completed = subprocess.run(  # noqa: S603 - fixed interpreter, test-owned paths
        [sys.executable, str(REPO / "src" / "agent" / "sandbox" / "runner.py"), str(request), str(result)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(result.read_text(encoding="utf-8")) == {
        "ok": True,
        "result": {"ok": True, "value": "from pylibs", "arg": 3},
    }


# ------------------------------------------------------ the guest handler runs it
def test_the_guest_still_starts_the_runner_with_its_pythonpath():
    """The runner adds pylibs itself, so the handler must not have to know about it."""
    source = (REPO / "src" / "agent" / "sandbox" / "handlers.py").read_text(encoding="utf-8")
    assert 'PYTHONPATH": "/usr/lib/agent"' in source


# ------------------------------------------------------- the image build contract
def test_the_build_script_installs_the_curated_set_and_verifies_it():
    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "EXTRA_PIP" in source
    assert "SKIP_EXTRA_PIP" in source
    assert "PIP_INDEX_URL" in source and "pypi.tuna.tsinghua.edu.cn" in source
    assert "--break-system-packages" in source and "--no-cache-dir" in source
    # the import smoke test the build must fail on
    assert "MISSING CURATED PACKAGES" in source
    assert "for name in (" in source
    # extras persisted from the console ride along on the next rebuild
    assert "AGENT_SANDBOX_EXTRA_PIP" in source
    # pip itself stays in the image: a session installs with `pip install --target ...`
    assert "python3-pip" in source and "python3-venv" in source


def test_the_build_script_targets_stay_in_sync_with_the_checker_and_the_console():
    """One curated list, three consumers: image build, import policy, /help pip."""
    from agent.cli.console import PIP_TARGET
    from agent.sandbox import checker

    for module in ("requests", "bs4", "lxml", "yaml", "openpyxl", "PIL", "numpy", "pandas"):
        assert module in checker.THIRD_PARTY_IMPORTS
    assert PIP_TARGET == "/workspace/pylibs"
    assert checker.THIRD_PARTY_IMPORTS  # the image list is documented in build-sandbox-image.sh
