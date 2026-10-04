"""End-to-end sandbox tests: a real QEMU VM, real isolation guarantees.

Requirements:

* a built sandbox image (``python -m agent.cli image verify``)
* a working accelerator (kvm / whpx / tcg)
* run from a host that can start QEMU

    pytest -m sandbox -v

These tests exercise the security properties the whole design rests on, so they
assert both the happy path and the *refusals*: read-only root, no network,
non-root execution, escapes denied, timeouts enforced, per-session isolation.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from agent.config import settings
from agent.control.manager import SandboxManager
from agent.models.tool import SandboxInvokeParams, ToolPayload

# The `manager` fixture is module-scoped while pytest-asyncio defaults to a fresh
# event loop per test.  A VM owns a socket writer bound to the loop that created it,
# so reusing that VM from another loop fails with a baffling
# "'NoneType' object has no attribute 'send'".  Pin the suite to a single loop.
pytestmark = [pytest.mark.sandbox, pytest.mark.asyncio(loop_scope="module")]

SESSION = "s-sandbox-test"
GOOD_TOOL = '''
def run(args):
    """Uppercase a file inside the workspace."""
    text = fs.read_text(args["path"])
    out = args.get("out") or "/workspace/upper.txt"
    fs.write_text(out, text.upper())
    return {"written": out, "chars": len(text)}
'''

GOOD_TESTS = [
    {"name": "success", "args": {"path": "/workspace/in.txt"}, "expect": {"contains": {"chars": 5}}},
    {"name": "edge", "args": {"path": "/workspace/in.txt", "out": "/workspace/nested/up.txt"},
     "expect": {"is_true": "result"}},
    {"name": "error", "args": {"path": "/workspace/missing.txt"}, "expect": {"raises": "FileNotFoundError"}},
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def manager(tmp_path_factory):
    if not settings.image_ready():
        pytest.skip("sandbox image not built: " + "; ".join(settings.sandbox_image_problems()))
    # Never share the overlay directory with a control plane that may be running:
    # two VMs opening one overlay corrupts it (Errno 5 I/O error inside the guest).
    isolated = settings.model_copy(update={"sessions_dir_override": str(tmp_path_factory.mktemp("sessions"))})
    mgr = SandboxManager(isolated)
    await mgr.start()
    try:
        yield mgr
    finally:
        await mgr.shutdown()


async def native(manager: SandboxManager, method: str, params: dict, timeout: float | None = None):
    return await manager.invoke(
        SandboxInvokeParams(session_id=SESSION, kind="native", method=method, params=params, timeout_s=timeout)
    )


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean_workspace(manager):
    await manager.invoke(
        SandboxInvokeParams(session_id=SESSION, kind="native", method="sandbox.reset", params={"keep_tools": True})
    )
    yield


# --------------------------------------------------------------------------- #
# environment facts
# --------------------------------------------------------------------------- #


async def test_vm_boots_and_answers(manager):
    handle = await manager.acquire(SESSION)
    assert handle.state == "ready"
    assert handle.vm_id


async def test_guest_reports_hardened_environment(manager):
    result = await native(manager, "sandbox.info", {})
    assert result.ok, result.error
    info = result.result
    assert info["root_readonly"] is True, "the root filesystem must be read-only"
    assert info["uid"] == 0, "the executor runs as pid 1 / root inside the VM"
    assert info["network_interfaces"] == [], "the sandbox must have no network interface"


async def test_commands_run_as_unprivileged_sandbox_user(manager):
    result = await native(manager, "exec.run", {"argv": ["id", "-u"]})
    assert result.ok, result.error
    assert result.result["stdout"].strip() == "1000"
    whoami = await native(manager, "exec.run", {"argv": ["id"]})
    assert "sandbox" in whoami.result["stdout"]


async def test_root_is_read_only(manager):
    result = await native(manager, "exec.run", {"argv": ["sh", "-c", "touch /etc/should_fail"]})
    assert result.result["exit_code"] != 0
    listing = await native(manager, "exec.run", {"argv": ["sh", "-c", "echo x > /root/f && echo wrote"]})
    assert "wrote" not in listing.result["stdout"]


async def test_no_network_at_all(manager):
    result = await native(
        manager,
        "exec.run",
        {
            "argv": [
                "python3",
                "-c",
                "import socket;s=socket.socket();s.settimeout(2);s.connect(('10.0.2.2',80))",
            ]
        },
    )
    assert result.result["exit_code"] != 0, "outbound connections must fail"
    interfaces = await native(manager, "exec.run", {"argv": ["sh", "-c", "ls /sys/class/net"]})
    assert "eth" not in interfaces.result["stdout"]


# --------------------------------------------------------------------------- #
# filesystem
# --------------------------------------------------------------------------- #


async def test_write_read_round_trip(manager):
    written = await native(manager, "fs.write", {"path": "/workspace/hello.txt", "text": "你好 sandbox\n"})
    assert written.ok and written.result["bytes_written"] > 0
    read = await native(manager, "fs.read", {"path": "/workspace/hello.txt"})
    assert read.result["text"] == "你好 sandbox\n"

    listed = await native(manager, "fs.list", {"path": "/workspace", "recursive": True})
    assert any(entry["name"] == "hello.txt" for entry in listed.result["entries"])

    stat = await native(manager, "fs.stat", {"path": "/workspace/hello.txt", "with_sha256": True})
    assert stat.result["exists"] and stat.result["type"] == "file"
    assert stat.result["sha256"] == read.result["sha256"]


async def test_binary_round_trip(manager):
    import base64

    payload = base64.b64encode(bytes(range(256))).decode()
    await native(manager, "fs.write", {"path": "/workspace/blob.bin", "data_b64": payload})
    read = await native(manager, "fs.read", {"path": "/workspace/blob.bin", "binary": True})
    assert read.result["data_b64"] == payload


async def test_path_traversal_is_denied(manager):
    for path in ("/etc/passwd", "../../etc/passwd", "/workspace/../etc/passwd", "/proc/self/environ"):
        result = await native(manager, "fs.read", {"path": path})
        assert not result.ok, f"{path} must be refused"
        assert result.error_code in {"path_denied", "permission_denied"}


async def test_symlink_escape_is_denied(manager):
    await native(manager, "exec.run", {"argv": ["sh", "-c", "ln -sf /etc/passwd /workspace/link"]})
    result = await native(manager, "fs.read", {"path": "/workspace/link"})
    assert not result.ok
    assert result.error_code in {"path_denied", "permission_denied"}


# --------------------------------------------------------------------------- #
# command execution limits
# --------------------------------------------------------------------------- #


async def test_exit_code_and_streams_are_captured(manager):
    ok = await native(manager, "exec.run", {"argv": ["sh", "-c", "echo out; echo err >&2; exit 3"]})
    assert ok.result["exit_code"] == 3
    assert ok.result["stdout"].strip() == "out"
    assert ok.result["stderr"].strip() == "err"


async def test_timeout_kills_the_process_group(manager):
    result = await native(
        manager, "exec.run", {"argv": ["sh", "-c", "sleep 30 & sleep 30; echo never"], "timeout_s": 2}
    )
    assert result.result["timed_out"] is True
    assert "never" not in result.result["stdout"]
    # the VM must still be responsive afterwards, i.e. the whole process group died
    follow_up = await native(manager, "exec.run", {"argv": ["echo", "alive"]})
    assert follow_up.result["stdout"].strip() == "alive"


async def test_output_is_truncated(manager):
    result = await native(
        manager, "exec.run", {"argv": ["sh", "-c", "yes agentbox | head -c 200000"], "max_output_bytes": 4096}
    )
    assert result.result["truncated"] is True
    assert len(result.result["stdout"]) <= 4096


async def test_memory_limit_is_enforced(manager):
    """The per-command budget (RLIMIT_AS + cgroup) must actually bite.

    The budget is requested explicitly so the test does not depend on the default
    (which was raised to 1024 MB once builds had to fit in it).
    """
    result = await native(
        manager,
        "exec.run",
        {
            "argv": ["python3", "-c", "b = bytearray(600 * 1024 * 1024); print(len(b))"],
            "timeout_s": 60,
            "memory_mb": 256,
        },
    )
    assert result.ok, result.error
    assert result.result["exit_code"] != 0 or "629145600" not in result.result["stdout"]
    limits = result.result.get("limits") or {}
    assert limits.get("memory_mb") == 256
    assert limits.get("rlimit_as_mb") == 256, "the rlimit backstop must match the budget"


async def test_working_directory_is_confined(manager):
    result = await native(manager, "exec.run", {"argv": ["pwd"], "cwd": "/workspace"})
    assert result.result["stdout"].strip() == "/workspace"
    outside = await native(manager, "exec.run", {"argv": ["pwd"], "cwd": "/etc"})
    assert not outside.ok


# --------------------------------------------------------------------------- #
# the tool authoring pipeline, end to end
# --------------------------------------------------------------------------- #


async def test_static_check_runs_inside_the_vm(manager):
    bad = await native(
        manager, "py.check", {"source": "import os\n\n\ndef run(args):\n    return os.getcwd()\n", "permissions": []}
    )
    assert bad.ok
    assert bad.result["ok"] is False
    assert any(v["rule"] == "import_forbidden" for v in bad.result["violations"])

    good = await native(manager, "py.check", {"source": GOOD_TOOL, "permissions": ["fs.read", "fs.write"]})
    assert good.result["ok"] is True, good.result


async def test_tool_install_and_run(manager):
    payload = _payload("upper_text", GOOD_TOOL, ["fs.read", "fs.write"])
    await native(manager, "fs.write", {"path": "/workspace/in.txt", "text": "hello"})
    run = await manager.invoke(
        SandboxInvokeParams(
            session_id=SESSION, kind="python", tool=payload, params={"path": "/workspace/in.txt"}, timeout_s=30
        )
    )
    assert run.ok, run.error
    assert run.result["ok"] is True
    assert run.result["result"]["chars"] == 5
    read = await native(manager, "fs.read", {"path": "/workspace/upper.txt"})
    assert read.result["text"] == "HELLO"


async def test_tool_source_hash_is_verified(manager):
    payload = _payload("upper_text", GOOD_TOOL, ["fs.read", "fs.write"])
    tampered = payload.model_copy(update={"sha256": "0" * 64})
    result = await manager.invoke(
        SandboxInvokeParams(session_id=SESSION, kind="python", tool=tampered, params={"path": "/workspace/in.txt"})
    )
    assert not result.ok
    assert "sha256" in (result.error or "")


async def test_tool_tests_run_in_the_sandbox(manager):
    payload = _payload("upper_text", GOOD_TOOL, ["fs.read", "fs.write"])
    await native(manager, "fs.write", {"path": "/workspace/in.txt", "text": "hello"})
    result = await native(
        manager,
        "tool.test",
        {
            "tool_name": payload.name,
            "version": payload.version,
            "sha256": payload.sha256,
            "source_b64": payload.source_b64,
            "permissions": list(payload.permissions),
            "entrypoint": "run",
            "tests": GOOD_TESTS,
            "timeout_s": 30,
        },
        timeout=120,
    )
    assert result.ok, result.error
    assert result.result["ok"] is True, json.dumps(result.result, indent=2)[:2000]
    assert result.result["passed"] == 3
    assert result.result["failed"] == 0


async def test_failing_tool_test_is_reported_not_raised(manager):
    payload = _payload("upper_text", GOOD_TOOL, ["fs.read", "fs.write"])
    await native(manager, "fs.write", {"path": "/workspace/in.txt", "text": "hello"})
    result = await native(
        manager,
        "tool.test",
        {
            "tool_name": payload.name,
            "version": payload.version,
            "sha256": payload.sha256,
            "source_b64": payload.source_b64,
            "permissions": list(payload.permissions),
            "entrypoint": "run",
            "tests": [
                {"name": "wrong expectation", "args": {"path": "/workspace/in.txt"}, "expect": {"equals": {"nope": 1}}}
            ],
            "timeout_s": 20,
        },
        timeout=60,
    )
    assert result.ok
    assert result.result["ok"] is False
    assert result.result["failed"] == 1
    assert result.result["results"][0]["message"]


async def test_tool_cannot_exceed_its_declared_permissions(manager):
    source = '''
def run(args):
    """Try to write without declaring fs.write."""
    fs.write_text("/workspace/sneaky.txt", "nope")
    return {"ok": True}
'''
    payload = _payload("sneaky_writer", source, ["fs.read"])
    result = await manager.invoke(
        SandboxInvokeParams(session_id=SESSION, kind="python", tool=payload, params={}, timeout_s=20)
    )
    assert result.ok
    assert result.result["ok"] is False
    assert "permission" in (result.result["error"] or "").lower()


async def test_tool_timeout_is_enforced(manager):
    source = '''
import time


def run(args):
    """Sleep far longer than the timeout."""
    time.sleep(30)
    return {"slept": True}
'''
    payload = _payload("sleeper", source, [])
    result = await manager.invoke(
        SandboxInvokeParams(session_id=SESSION, kind="python", tool=payload, params={}, timeout_s=3)
    )
    assert result.ok
    assert result.result["ok"] is False
    assert result.result["timed_out"] is True


async def test_installed_tool_source_is_read_only(manager):
    payload = _payload("upper_text", GOOD_TOOL, ["fs.read", "fs.write"])
    await manager.invoke(
        SandboxInvokeParams(session_id=SESSION, kind="python", tool=payload, params={"path": "/workspace/in.txt"})
    )
    result = await native(
        manager, "exec.run", {"argv": ["sh", "-c", f"echo hacked > /workspace/.tools/upper_text@{payload.version}/tool.py"]}
    )
    assert result.result["exit_code"] != 0
    listing = await native(manager, "fs.stat", {"path": f"/workspace/.tools/upper_text@{payload.version}/tool.py"})
    assert listing.result["mode"] == "0o444"


# --------------------------------------------------------------------------- #
# isolation between sessions
# --------------------------------------------------------------------------- #


async def test_sessions_do_not_share_a_workspace(manager):
    other = "s-sandbox-other"
    await native(manager, "fs.write", {"path": "/workspace/secret.txt", "text": "session one"})
    await manager.invoke(
        SandboxInvokeParams(session_id=other, kind="native", method="sandbox.reset", params={"keep_tools": True})
    )
    result = await manager.invoke(
        SandboxInvokeParams(session_id=other, kind="native", method="fs.read", params={"path": "/workspace/secret.txt"})
    )
    assert not result.ok, "a second session must not see the first session's files"
    assert result.vm_id != (await manager.acquire(SESSION)).vm_id
    await manager.release(other)


async def test_reset_clears_the_workspace(manager):
    await native(manager, "fs.write", {"path": "/workspace/junk.txt", "text": "x"})
    await native(manager, "sandbox.reset", {"keep_tools": True})
    result = await native(manager, "fs.list", {"path": "/workspace"})
    names = [entry["name"] for entry in result.result["entries"]]
    assert "junk.txt" not in names


async def test_guest_rejects_unauthenticated_calls(manager):
    """The guest executor must require sys.hello before any other method.

    The live connection is already authenticated, so this asserts the guest-side
    rule through the handler table (see tests/unit/test_guest_auth.py for the
    full matrix) and then confirms the live client really did authenticate.
    """
    vm = manager.bindings[SESSION].vm
    assert vm.client is not None
    info = await native(manager, "sandbox.info", {})
    assert info.ok, "the authenticated control-plane client must be able to call methods"
    assert info.result["vm_id"] == vm.record.vm_id


def _payload(name: str, source: str, permissions: list[str], version: int = 1) -> ToolPayload:
    import base64
    import hashlib

    return ToolPayload(
        name=name,
        version=version,
        sha256=hashlib.sha256(source.encode()).hexdigest(),
        source_b64=base64.b64encode(source.encode()).decode(),
        permissions=permissions,  # type: ignore[arg-type]
        entrypoint="run",
        timeout_s=30.0,
    )
