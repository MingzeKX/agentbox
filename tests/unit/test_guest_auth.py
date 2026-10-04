"""Guest executor internals that can be tested without booting a VM.

Covers the RPC authentication handshake, method routing and error mapping, the
path policy, the tool test assertion language and the output trimming rules.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Any

import pytest

from agent.sandbox import handlers, policy
from agent.sandbox.handlers import HandlerError, Methods, _decode_source, _evaluate

TOKEN = "t" * 64
VM_ID = "vm-unit-test"
PEER = "127.0.0.1:1234#0"


@pytest.fixture
def methods() -> Methods:
    return Methods(token=TOKEN, vm_id=VM_ID)


# --------------------------------------------------------------------------- #
# authentication
# --------------------------------------------------------------------------- #


def test_ping_is_allowed_before_authentication(methods):
    assert methods.dispatch("sys.ping", {}, PEER)["pong"] is True


@pytest.mark.parametrize("method", ["fs.read", "fs.list", "exec.run", "sandbox.info", "py.check"])
def test_methods_are_refused_before_hello(methods, method):
    with pytest.raises(HandlerError) as excinfo:
        methods.dispatch(method, {"path": "/workspace/x", "source": ""}, PEER)
    assert excinfo.value.code == 1000
    assert "sys.hello" in excinfo.value.message


def test_hello_rejects_a_wrong_token(methods):
    with pytest.raises(HandlerError) as excinfo:
        methods.dispatch("sys.hello", {"token": "wrong", "nonce": "n"}, PEER)
    assert excinfo.value.code == 1000


def test_hello_requires_a_nonce(methods):
    with pytest.raises(HandlerError) as excinfo:
        methods.dispatch("sys.hello", {"token": TOKEN}, PEER)
    assert excinfo.value.code == -32602


def test_hello_returns_a_verifiable_proof(methods):
    result = methods.dispatch("sys.hello", {"token": TOKEN, "nonce": "abc123"}, PEER)
    expected = hmac.new(TOKEN.encode(), f"abc123:{VM_ID}".encode(), hashlib.sha256).hexdigest()
    assert result["proof"] == expected
    assert result["vm_id"] == VM_ID
    assert result["protocol"] == 1
    assert "tool.install" in result["capabilities"]


def test_calls_are_allowed_after_authentication(methods):
    methods.dispatch("sys.hello", {"token": TOKEN, "nonce": "n"}, PEER)
    methods.authenticate(PEER)
    with pytest.raises(HandlerError) as excinfo:
        # /etc is denied by the path policy long before any filesystem access
        methods.dispatch("fs.read", {"path": "/etc/passwd"}, PEER)
    assert excinfo.value.code == 1008


def test_guest_without_a_token_never_authenticates():
    open_methods = Methods(token="", vm_id=VM_ID)
    with pytest.raises(HandlerError):
        open_methods.dispatch("sys.hello", {"token": "", "nonce": "n"}, PEER)


def test_unknown_method_is_reported(methods):
    methods.authenticate(PEER)
    with pytest.raises(HandlerError) as excinfo:
        methods.dispatch("fs.teleport", {}, PEER)
    assert excinfo.value.code == -32601


# --------------------------------------------------------------------------- #
# path policy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "/", "/root/.ssh/id_rsa", "/workspace/../etc/shadow", "/proc/self/environ", "/dev/vda"],
)
def test_policy_denies_paths_outside_the_workspace(path):
    with pytest.raises(policy.PathDenied):
        policy.resolve(path)


@pytest.mark.parametrize("path", ["/workspace/a.txt", "/workspace/.tools/x/tool.py", "relative.txt", "./sub/../a.txt"])
def test_policy_accepts_paths_inside_the_workspace(path):
    # is_within keeps the assertion portable between the Linux guest and the
    # Windows test host.
    assert policy.is_within(policy.resolve(path))


def test_policy_rejects_nul_bytes():
    with pytest.raises(policy.PathDenied):
        policy.resolve("/workspace/a\x00b")


def test_permission_helper_message_names_the_permission():
    with pytest.raises(policy.PathDenied) as excinfo:
        policy.require(set(), "fs.write")
    assert "fs.write" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# tool result handling
# --------------------------------------------------------------------------- #


def test_decode_source_prefers_plain_text_and_falls_back_to_base64():
    assert _decode_source({"source": "x = 1"}) == "x = 1"
    encoded = base64.b64encode(b"x = 2").decode()
    assert _decode_source({"source_b64": encoded}) == "x = 2"
    with pytest.raises(HandlerError):
        _decode_source({})


def test_evaluate_equals():
    ok, _ = _evaluate({"equals": {"a": 1}}, {"ok": True, "result": {"a": 1}})
    assert ok
    bad, message = _evaluate({"equals": {"a": 1}}, {"ok": True, "result": {"a": 2}})
    assert not bad and "expected" in message


def test_evaluate_contains():
    assert _evaluate({"contains": {"a": 1}}, {"ok": True, "result": {"a": 1, "b": 2}})[0]
    ok, message = _evaluate({"contains": {"a": 1}}, {"ok": True, "result": {"a": 2}})
    assert not ok and "expected 1" in message
    assert _evaluate({"contains": {"a": 1}}, {"ok": True, "result": "nope"})[0] is False


def test_evaluate_raises():
    outcome = {"ok": False, "error": "ValueError: bad", "error_type": "ValueError"}
    assert _evaluate({"raises": "ValueError"}, outcome)[0]
    assert _evaluate({"raises": "KeyError"}, outcome)[0] is False
    assert _evaluate({"raises": "ValueError"}, {"ok": True, "result": 1})[0] is False


def test_evaluate_is_true():
    assert _evaluate({"is_true": "result"}, {"ok": True, "result": {"a": 1}})[0]
    assert _evaluate({"is_true": "result"}, {"ok": True, "result": {}})[0] is False


def test_evaluate_requires_the_call_to_succeed_first():
    ok, message = _evaluate({"equals": 1}, {"ok": False, "error": "boom", "error_type": "RuntimeError"})
    assert not ok and "boom" in message


def test_evaluate_without_an_assertion_is_a_failure_not_a_crash():
    ok, message = _evaluate({}, {"ok": True, "result": 1})
    assert ok is False
    assert "no supported assertion" in message


def test_handler_error_is_not_an_rpc_success():
    assert issubclass(HandlerError, Exception)
    assert handlers.PROTOCOL_VERSION == 1


@pytest.mark.parametrize("name", ["../evil", "/etc/passwd", "a/b", "name@1", "", "UPPER", "a" * 42, "..", "x. y"])
def test_tool_install_rejects_dangerous_names(name):
    with pytest.raises(HandlerError) as excinfo:
        handlers._install_source(name, 1, "def run(args):\n    return {}\n", "run", [])
    assert excinfo.value.code == -32602


def test_tool_install_accepts_namespaced_names_without_touching_the_disk(monkeypatch):
    """The name check happens before any filesystem access, so this is portable."""
    captured: dict[str, Any] = {"writes": []}

    def fake_makedirs(path, exist_ok=False):
        captured["dir"] = path

    class DummyFile:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def write(self, data):
            captured["writes"].append((self.path, data))

    def fake_open(path, *args, **kwargs):
        return DummyFile(path)

    monkeypatch.setattr(handlers.os, "makedirs", fake_makedirs)
    monkeypatch.setattr(handlers.os, "chmod", lambda *a, **k: None)
    monkeypatch.setattr("builtins.open", fake_open)

    path = handlers._install_source("fs.read", 3, "def run(args):\n    return {}\n", "run", [])
    # The guest is Linux; os.path.join keeps this assertion portable to the test host.
    expected_dir = os.path.join("/workspace/.tools", "fs.read@3")
    assert path == os.path.join(expected_dir, "tool.py")
    assert captured["dir"] == expected_dir
    tool_path, source = captured["writes"][0]
    assert tool_path == path
    assert source.startswith("def run(args)")
