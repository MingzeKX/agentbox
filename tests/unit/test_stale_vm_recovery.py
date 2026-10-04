"""A stale sandbox VM must be recycled, not retried forever.

Live failure: a warm VM sat idle for ~37 minutes, the guest executor exited on its own
idle timer, init restarted it with an empty authenticated-peer set, and every call
after that returned ``unauthorized: unauthenticated peer: call sys.hello first`` -- the
agent burned 14 steps hunting for a ``sys.hello`` tool that does not exist.

Two fixes are covered here:
  * the guest no longer restarts its executor (see sandbox/init.py, sandbox/executor.py)
  * the control plane evicts a VM that answers with a "stale" error code, so the next
    call binds a fresh VM while the session overlay (and therefore the workspace) stays
"""

from __future__ import annotations

import pytest

from agent.control.manager import STALE_VM_CODES, SandboxManager
from agent.models.tool import SandboxInvokeParams


class FakeVm:
    def __init__(self, vm_id: str = "vm-stale") -> None:
        self.record = type("Record", (), {"vm_id": vm_id, "session_id": None, "state": "ready", "booted_at": 0.0})()
        self.stopped = 0
        self.replies: list[dict] = []
        self.record.commands_run = 0

    @property
    def alive(self) -> bool:
        return True

    async def stop(self, force: bool = False) -> None:  # noqa: ARG002
        self.stopped += 1

    async def destroy(self) -> None:
        self.stopped += 1

    async def reset_workspace(self) -> None:
        return None

    async def call(self, method, params=None, timeout=None):  # noqa: ANN001, ARG002
        reply = self.replies.pop(0) if self.replies else {"ok": True}
        if isinstance(reply, Exception):
            raise reply
        return reply


def build_manager(monkeypatch, vm: FakeVm) -> SandboxManager:
    from agent.config import Settings

    manager = SandboxManager(Settings(sandbox_dir=str(__import__("tempfile").mkdtemp())))
    monkeypatch.setattr(manager, "claim", lambda session_id: _claim(manager, vm, session_id))
    return manager


async def _claim(manager: SandboxManager, vm: FakeVm, session_id: str) -> FakeVm:
    from agent.control.manager import SessionBinding

    manager.bindings[session_id] = SessionBinding(session_id=session_id, vm=vm)  # type: ignore[arg-type]
    manager.all_vms[vm.record.vm_id] = vm  # type: ignore[assignment]
    return vm


@pytest.mark.asyncio
async def test_unauthorized_error_recycles_the_vm(monkeypatch):
    from agent.models.protocol import ErrorCode, RpcError

    vm = FakeVm()
    manager = build_manager(monkeypatch, vm)
    vm.replies = [RpcError(ErrorCode.UNAUTHORIZED, "unauthenticated peer: call sys.hello first")]

    result = await manager.invoke(
        SandboxInvokeParams(session_id="s1", kind="native", method="exec.run", params={"argv": ["true"]})
    )

    assert result.ok is False
    assert result.error_code == "unauthorized"
    assert "recycled" in (result.error or "")
    assert vm.stopped >= 1, "the stale VM must be stopped"
    assert "s1" not in manager.bindings, "the binding must be dropped so the next call rebinds"


@pytest.mark.asyncio
async def test_healthy_calls_do_not_recycle(monkeypatch):
    vm = FakeVm()
    manager = build_manager(monkeypatch, vm)
    vm.replies = [{"exit_code": 0, "stdout": "ok"}]

    result = await manager.invoke(
        SandboxInvokeParams(session_id="s1", kind="native", method="exec.run", params={"argv": ["true"]})
    )

    assert result.ok is True
    assert vm.stopped == 0
    assert "s1" in manager.bindings


def test_stale_codes_are_the_expected_ones():
    assert {"unauthorized", "unavailable"} <= STALE_VM_CODES
