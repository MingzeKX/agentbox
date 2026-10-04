"""`/pip`: installing packages into the current session's sandbox from the console.

The pain point: a package the model wants is not baked into the image, and the sandbox root
is read-only with a per-session /workspace.  `/pip install` is the session answer (install
into /workspace/pylibs, which the guest runner puts on sys.path) and `/pip persist` is the
durable answer (write AGENT_SANDBOX_EXTRA_PIP into the VM's .env so the next image build has
it).  These tests pin the RPC the console sends, that it never touches /admin/config, and
that a failure explains the real reason.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from rich.console import Console

from agent.cli.console import (
    PIP_INDEX_URL,
    PIP_PERSIST_KEY,
    PIP_REPORT_MARKER,
    PIP_TARGET,
    ConsoleState,
    SlashConsole,
    complete,
)

LIST_PAYLOAD = {
    "packages": [{"name": "requests", "version": "2.32.3"}, {"name": "pandas", "version": "2.2.3"}],
    "total_bytes": 15_728_640,
    "target": PIP_TARGET,
}


class FakePipConsole(SlashConsole):
    """Records the control-plane calls and the .env write instead of making them."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.rpc_calls: list[tuple[str, dict[str, Any]]] = []
        self.persisted: list[dict[str, Any]] = []
        self.outcomes: list[dict[str, Any]] = []
        self.env_value: str | None = None
        self.persist_error: str | None = None

    def _sandbox_exec(self, argv: list[str], *, timeout: float = 300.0) -> dict[str, Any]:
        self.rpc_calls.append(("exec.run", {"argv": argv, "timeout": timeout}))
        if not self.outcomes:
            # No queued outcome: fall through to the real RPC path so a test can stub the
            # control plane itself (that is how the session id / envelope shape is pinned).
            return super()._sandbox_exec(argv, timeout=timeout)
        return self.outcomes.pop(0)

    def _env_value(self, name: str) -> str | None:
        self.rpc_calls.append(("env_value", {"name": name}))
        return self.env_value

    def _persist(self, keys: dict[str, Any]) -> None:
        if self.persist_error:
            raise RuntimeError(self.persist_error)
        self.persisted.append(dict(keys))


@pytest.fixture
def shell():
    console = Console(record=True, width=140, no_color=True, force_terminal=False)
    state = ConsoleState(session_id="s-abc123")
    return FakePipConsole(console, state, console), console, state


def output(console: Console) -> str:
    return console.export_text()


def report(payload: dict[str, Any]) -> str:
    """What the guest probe writes: the marker, then its JSON (plus some pip chatter)."""
    return f"some pip chatter\n{PIP_REPORT_MARKER}{json.dumps(payload)}\n"


def ok(stdout: str = "", stderr: str = "", exit_code: int = 0, timed_out: bool = False) -> dict[str, Any]:
    """One `exec.run` outcome, exactly as the guest returns it (flat, not nested)."""
    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "duration_ms": 5,
        "truncated": False,
    }


# ------------------------------------------------------------------- discovery
def test_pip_is_discoverable_and_completes_its_subcommands():
    from agent.cli.console import HELP

    assert "/pip" in complete("/pi")
    assert complete("/pip ") == ["/pip list", "/pip install", "/pip persist"]
    assert "/pip" in HELP


def test_pip_has_help_text_in_chinese():
    from agent.cli.console import COMMAND_HELP

    text = COMMAND_HELP["pip"]
    assert "/pip install" in text and "/pip persist" in text and "/pip list" in text
    assert "只读" in text, "the help must state that the sandbox root is read-only"
    assert PIP_INDEX_URL in text, "the help must name the mirror it uses"


# ----------------------------------------------------------------------- /pip list
def test_pip_list_shows_the_packages_and_the_total_size(shell):
    sh, console, _ = shell
    sh.outcomes.append(ok(stdout=report(LIST_PAYLOAD)))

    assert sh.handle("/pip list") is True

    argv = sh.rpc_calls[0][1]["argv"]
    assert argv[:2] == ["/usr/bin/python3", "-c"]
    assert "packages" in argv[2] and "total_bytes" in argv[2], "the probe is our own script"
    assert "--format" not in argv, "not pip's CLI, so nothing to parse out of its chatter"
    text = output(console)
    assert "requests" in text and "2.32.3" in text
    assert "pandas" in text
    assert "15.0" in text and "MB" in text, "the total size is reported"
    assert sh.persisted == [], "listing must not write .env"


def test_pip_list_on_an_empty_target_explains_the_next_step(shell):
    sh, console, _ = shell
    sh.outcomes.append(ok(stdout=report({"packages": [], "total_bytes": 0})))

    sh.handle("/pip")

    text = output(console)
    assert "还没有包" in text
    assert "/pip install" in text
    assert "预装" in text, "the operator should know the image already ships a set"


def test_pip_list_reports_an_unreachable_sandbox(shell, monkeypatch):
    sh, console, _ = shell
    import agent.cli.console as console_mod

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise SystemExit("control plane unreachable at http://127.0.0.1:8765/rpc")

    monkeypatch.setattr(console_mod, "_control_rpc", boom)
    sh.handle("/pip list")
    assert "控制平面不可达" in output(console)


# -------------------------------------------------------------------- /pip install
def test_pip_install_targets_the_session_directory_with_the_mirror(shell):
    sh, console, _ = shell
    sh.outcomes.append(ok(stdout="Successfully installed pyftpdlib-2.0.1"))

    assert sh.handle("/pip install pyftpdlib") is True

    argv = sh.rpc_calls[0][1]["argv"]
    assert argv[:2] == ["/usr/bin/python3", "-m"]
    assert argv[2:5] == ["pip", "install", "--no-cache-dir"]
    assert argv[argv.index("--target") + 1] == PIP_TARGET
    assert argv[argv.index("-i") + 1] == PIP_INDEX_URL
    assert argv[-1] == "pyftpdlib"
    text = output(console)
    assert "Successfully installed pyftpdlib-2.0.1" in text, "pip's own output is shown verbatim"
    assert PIP_TARGET in text
    assert "/pip persist pyftpdlib" in text, "the durable next step is offered"
    assert sh.persisted == [], "an install is a runtime action, not a .env rewrite"


def test_pip_install_keeps_the_requested_order_and_every_name(shell):
    sh, console, _ = shell
    sh.outcomes.append(ok())
    sh.handle("/pip install httpx tenacity")
    argv = sh.rpc_calls[0][1]["argv"]
    assert argv[-2:] == ["httpx", "tenacity"]


def test_pip_install_needs_a_package_name(shell):
    sh, console, _ = shell
    sh.handle("/pip install")
    assert "用法" in output(console)
    assert sh.rpc_calls == []


def test_pip_install_never_touches_admin_config(shell):
    sh, _, _ = shell
    sh.outcomes.append(ok())
    sh.handle("/pip install pyftpdlib")
    assert sh.persisted == []
    assert [name for name, _ in sh.rpc_calls] == ["exec.run"]


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        ("", "ERROR: Could not find a version that satisfies the requirement nope", "源里没有这个包"),
        ("", "ERROR: No matching distribution found for nope", "源里没有这个包"),
        ("", "WARNING: Retrying... Temporary failure in name resolution", "/net on"),
        ("", "ERROR: Permission denied: '/workspace/pylibs'", "可写"),
    ],
)
def test_pip_install_failures_name_the_real_reason(shell, stdout, stderr, expected):
    sh, console, _ = shell
    sh.outcomes.append(ok(stdout=stdout, stderr=stderr, exit_code=1))

    sh.handle("/pip install nope")

    text = output(console)
    assert "装包失败" in text
    assert expected in text


def test_pip_install_timeout_is_explained(shell):
    sh, console, _ = shell
    sh.outcomes.append(ok(exit_code=124, timed_out=True))
    sh.handle("/pip install tensorflow")
    text = output(console)
    assert "超时" in text
    assert "重建镜像" in text


# -------------------------------------------------------------------- /pip persist
def test_pip_persist_writes_the_env_key_and_merges_what_is_there(shell):
    sh, console, _ = shell
    sh.env_value = "httpx tenacity"

    assert sh.handle("/pip persist pyftpdlib") is True

    assert sh.persisted == [{PIP_PERSIST_KEY: "httpx tenacity pyftpdlib"}]
    text = output(console)
    assert PIP_PERSIST_KEY in text
    assert "下次重建镜像即永久生效" in text
    assert "build-sandbox-image.sh" in text, "the exact command must be in the message"
    assert PIP_TARGET in text, "and how to use it right now"


def test_pip_persist_does_not_duplicate_a_name(shell):
    sh, _, _ = shell
    sh.env_value = "pyftpdlib"
    sh.handle("/pip persist pyftpdlib,httpx")
    assert sh.persisted == [{PIP_PERSIST_KEY: "pyftpdlib httpx"}]


def test_pip_persist_warns_when_the_current_list_could_not_be_read(shell):
    sh, console, _ = shell
    sh.env_value = None
    sh.handle("/pip persist httpx")
    assert sh.persisted == [{PIP_PERSIST_KEY: "httpx"}]
    assert "覆盖写" in output(console)


def test_pip_persist_reports_a_failed_env_write(shell):
    sh, console, _ = shell
    sh.persist_error = "无法持久化：缺少 ssh 客户端或 var/vm_key（请改在 VM 内运行）"
    sh.handle("/pip persist httpx")
    text = output(console)
    assert "无法持久化" in text
    assert sh.persisted == []


def test_pip_persist_needs_a_package_name(shell):
    sh, console, _ = shell
    sh.handle("/pip persist")
    assert "用法" in output(console)
    assert sh.persisted == []


def test_pip_rejects_an_unknown_action(shell):
    sh, console, _ = shell
    sh.handle("/pip explode")
    text = output(console)
    assert "用法" in text
    assert sh.rpc_calls == [] and sh.persisted == []


# --------------------------------------------------------------------- the session
def test_pip_acts_on_the_current_session_and_falls_back_to_default(shell):
    sh, _, state = shell
    assert sh._sandbox_session() == "s-abc123"
    state.session_id = None
    assert sh._sandbox_session() == "default"


def test_pip_install_passes_the_session_to_the_control_plane(shell, monkeypatch):
    """The session id is what binds the install to the sandbox the agent is using."""
    sh, _, _ = shell
    import agent.cli.console as console_mod

    seen: dict[str, Any] = {}

    def fake_rpc(method: str, params: dict[str, Any] | None = None, timeout: float = 120.0) -> dict[str, Any]:
        seen.update(method=method, params=params)
        # the control plane's SandboxInvokeResult envelope around exec.run's own outcome
        return {"result": {"ok": True, "result": {"exit_code": 0, "stdout": "installed", "stderr": ""}}}

    monkeypatch.setattr(console_mod, "_control_rpc", fake_rpc)
    sh.handle("/pip install httpx")

    assert seen["method"] == "sandbox.invoke"
    assert seen["params"]["session_id"] == "s-abc123"
    assert seen["params"]["kind"] == "native"
    assert seen["params"]["method"] == "exec.run"
    assert seen["params"]["params"]["argv"][-1] == "httpx"
    assert sh.persisted == [], "the AI service's /admin/config is not involved at all"
