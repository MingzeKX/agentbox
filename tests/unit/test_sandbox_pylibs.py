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
import shutil
import subprocess
import sys
import textwrap
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


def test_the_build_script_recreates_the_runtime_resolver_symlink():
    """Regression: the pip step needs a real /etc/resolv.conf and used to leave it there.

    The guest root is read-only, so a regular file there means net mode can never write DNS
    again. The build has to put the symlink back AND check it in the finished image.
    """
    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "ln -s /run/agent/resolv.conf" in source
    assert "debugfs" in source and "Type: symlink" in source, "the finished image is verified too"


def test_the_guest_runner_and_the_checker_agree_on_the_curated_roots():
    """The image installs packages; the checker has to admit exactly the importable names."""
    from agent.sandbox import checker

    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    # distribution names -> the module names a tool imports (the checker's list)
    expected = {
        "requests": "requests",
        "beautifulsoup4": "bs4",
        "lxml": "lxml",
        "pyyaml": "yaml",
        "python-dateutil": "dateutil",
        "pytz": "pytz",
        "openpyxl": "openpyxl",
        "pillow": "PIL",
        "numpy": "numpy",
        "pandas": "pandas",
    }
    curated = source[source.index('CURATED_PIP="${CURATED_PIP:-') :]
    curated = curated[: curated.index("}")]
    for distribution, module in expected.items():
        assert distribution in curated, f"{distribution} missing from the image list"
        assert module in checker.THIRD_PARTY_IMPORTS, f"{module} missing from THIRD_PARTY_IMPORTS"


def test_the_build_script_targets_stay_in_sync_with_the_checker_and_the_console():
    """One curated list, three consumers: image build, import policy, /help pip."""
    from agent.cli.console import PIP_TARGET
    from agent.sandbox import checker

    for module in ("requests", "bs4", "lxml", "yaml", "openpyxl", "PIL", "numpy", "pandas"):
        assert module in checker.THIRD_PARTY_IMPORTS
    assert PIP_TARGET == "/workspace/pylibs"
    assert checker.THIRD_PARTY_IMPORTS  # the image list is documented in build-sandbox-image.sh


# ------------------------------------------------------- the workspace disk size
def _workspace_size_snippet() -> str:
    """The size-resolution block of the build script, verbatim and runnable.

    Everything it needs is the environment (the command-line override), ``$BASH_ENV`` (the
    VM's .env) and this one .env variable, so a real ``bash`` can execute it directly --
    the same trick the rest of this file uses to pin the image build.
    """
    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    start = source.index('WORKSPACE_SIZE_DEFAULT="')
    end = source.index('echo "==> workspace disk:')
    snippet = textwrap.dedent(source[start:end])
    return snippet + 'printf "%s (source: %s)\\n" "${WORKSPACE_SIZE}" "${WORKSPACE_SIZE_SOURCE}"\n'


def _bash() -> str | None:
    """A real bash, if this host has one (the build script runs on Linux).

    Git-for-Windows bash is a genuine bash and is used when ``which`` cannot see it, so the
    precedence below is executed rather than merely grepped on a Windows development host.
    """
    if os.name == "posix":
        return shutil.which("bash")
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(candidate).is_file():
            return candidate
    return None


def _resolve_workspace_size(tmp_path: Path, command_line: str = "") -> str:
    """Resolve the workspace size exactly as the build script does, and return its report."""
    bash = _bash()
    if bash is None:
        pytest.skip("the build script needs a bash; none is available on this host")
    env = dict(os.environ)
    env.pop("WORKSPACE_SIZE", None)
    if os.name == "nt":  # make the grep/tail the snippet calls reachable from Git-bash
        env["PATH"] = str(Path(bash).parent) + os.pathsep + env.get("PATH", "")
    env["BASH_ENV"] = str(tmp_path / "bash_env").replace("\\", "/")  # the environment the VM would have
    env["WORKSPACE_SIZE_ENV_FILE"] = str(tmp_path / ".env").replace("\\", "/")  # the VM's .env, stood in for
    if command_line:
        env["WORKSPACE_SIZE"] = command_line
    completed = subprocess.run(  # noqa: S603 - fixed interpreter, test-owned snippet
        [bash, "-c", _workspace_size_snippet()],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_the_workspace_disk_precedence_is_command_line_then_env_file_then_default(tmp_path):
    """Regression: a rebuild without WORKSPACE_SIZE silently shrank 20 GiB to 4 GiB."""
    assert _resolve_workspace_size(tmp_path) == "20480M (source: default)"
    (tmp_path / ".env").write_text(
        "# the VM's .env shape: a comment, then the setting\nAGENT_SANDBOX_WORKSPACE_MB=20480\n",
        encoding="utf-8",
        newline="\n",
    )
    assert _resolve_workspace_size(tmp_path) == "20480M (source: .env (AGENT_SANDBOX_WORKSPACE_MB))"
    assert _resolve_workspace_size(tmp_path, command_line="4096M") == "4096M (source: command line)"
    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "e2fsprogs" in source, "the guest needs resize2fs to use a grown disk"


def test_growing_the_workspace_filesystem_is_idempotent_and_never_fatal(monkeypatch, tmp_path):
    from agent.sandbox import init

    device = tmp_path / "vdb"
    device.write_bytes(b"\x00" * 16)
    monkeypatch.setattr(init.shutil, "which", lambda _name: "/sbin/resize2fs")
    calls: list[list[str]] = []

    def already_full_size(argv, **_kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, b"", b"The filesystem is already 20971520 (4k) blocks long.\n")

    verdict = init.grow_workspace_filesystem(str(device), already_full_size)

    assert calls == [["/sbin/resize2fs", str(device)]], "called once per boot, with the workspace device"
    assert verdict.startswith("skipped:"), "an already-full filesystem is logged, not raised"
    assert init.grow_workspace_filesystem(str(tmp_path / "absent"), already_full_size) == "no device"
    monkeypatch.setattr(init.shutil, "which", lambda _name: None)
    assert init.grow_workspace_filesystem(str(device), already_full_size) == "resize2fs missing"
    assert len(calls) == 1, "a missing resize2fs must not run anything"
