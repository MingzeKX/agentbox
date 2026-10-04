"""Sandbox pool: session -> VM binding, warm pool, idle reaping, eviction.

Invariants
----------
* at most one VM per session, at most ``sandbox_max_vms`` VMs overall
* ``sandbox_pool_size`` VMs are kept booted and unbound so the first tool call of
  a session does not pay the boot cost (this matters a lot under WHPX/TCG)
* a VM is never reused across sessions without its workspace being reset
* releasing a session stops the VM along with its overlay; the disk file survives
  so a later session with the same id keeps its files
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from agent.config import Settings
from agent.control.host.base import HostBackend, build_backend
from agent.control.vm import QemuVm, SandboxBootError
from agent.models.protocol import ErrorCode, RpcError
from agent.models.tool import (
    PoolStatus,
    SandboxHandle,
    SandboxInvokeParams,
    SandboxInvokeResult,
    ToolPayload,
    VmStatus,
)

log = logging.getLogger(__name__)

#: error codes that mean "this VM can never answer again" -> recycle it
STALE_VM_CODES = {"unauthorized", "unavailable", "sandbox_error"}


@dataclass
class SessionBinding:
    session_id: str
    vm: QemuVm
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    invocations: int = 0


class SandboxManager:
    def __init__(self, settings: Settings, host: HostBackend | None = None) -> None:
        self.settings = settings
        self.host = host or build_backend(settings)
        self.accel = self.host.accel_args()
        self.bindings: dict[str, SessionBinding] = {}
        self.warm: list[QemuVm] = []
        self.all_vms: dict[str, QemuVm] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None
        self._warm_task: asyncio.Task[None] | None = None
        self._starting = False

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self.settings.ensure_dirs()
        problems = self.settings.sandbox_image_problems()
        if problems:
            log.warning(
                "sandbox image is not ready (%s); VMs will not start until you run "
                "'python -m agent.cli image build'",
                "; ".join(problems),
            )
            return
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop(), name="sandbox-reaper")
        # Warm the pool in the background: booting a VM takes seconds (or minutes
        # under TCG) and must not block the HTTP app's startup, otherwise /health
        # is unreachable exactly when an operator needs it.
        self._warm_task = asyncio.create_task(self._top_up_pool(), name="sandbox-warmup")

    async def shutdown(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reaper
            self._reaper = None
        for binding in list(self.bindings.values()):
            with contextlib.suppress(Exception):
                await binding.vm.stop(force=True)
        if self._warm_task is not None:
            self._warm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._warm_task
            self._warm_task = None
        for vm in self.warm:
            with contextlib.suppress(Exception):
                await vm.stop(force=True)
        self.bindings.clear()
        self.warm.clear()
        self.all_vms.clear()

    # ------------------------------------------------------------------ pool
    async def _top_up_pool(self) -> None:
        """Keep ``sandbox_pool_size`` unbound VMs booted (best effort)."""
        async with self._lock:
            if self._starting:
                return
            self._starting = True
        try:
            while True:
                async with self._lock:
                    if len(self.warm) >= self.settings.sandbox_pool_size:
                        return
                    if len(self.all_vms) >= self.settings.sandbox_max_vms:
                        return
                try:
                    vm = QemuVm(settings=self.settings, host=self.host)
                    await vm.start()
                except SandboxBootError as exc:
                    log.error("cannot pre-boot sandbox: %s", exc)
                    return
                async with self._lock:
                    self.warm.append(vm)
                    self.all_vms[vm.record.vm_id] = vm
                log.info("warm sandbox %s ready (%d/%d)", vm.record.vm_id, len(self.warm), self.settings.sandbox_pool_size)
        finally:
            async with self._lock:
                self._starting = False

    async def _evict_lru(self) -> None:
        """Free a slot by stopping the least recently used session VM."""
        async with self._lock:
            if not self.bindings:
                return
            victim = min(self.bindings.values(), key=lambda b: b.last_used)
        log.warning("evicting %s (session %s) to respect sandbox_max_vms", victim.vm.record.vm_id, victim.session_id)
        async with self._lock:
            self.bindings.pop(victim.session_id, None)
            self.all_vms.pop(victim.vm.record.vm_id, None)
        await victim.vm.stop()
        await victim.vm.destroy()

    async def claim(self, session_id: str) -> QemuVm:
        """Bind a VM to ``session_id``.

        A previously used session gets a fresh VM on the *same* overlay disk, so
        its files survive a VM restart.  A brand new session takes a warm VM and
        wipes its scratch workspace first.
        """
        async with self._lock:
            binding = self.bindings.get(session_id)
            if binding is not None and binding.vm.alive:
                binding.last_used = time.time()
                return binding.vm
            if binding is not None:
                self.bindings.pop(session_id, None)
                self.all_vms.pop(binding.vm.record.vm_id, None)

        existing_overlay = (self.settings.sessions_dir / session_id / "workspace.qcow2").exists()
        vm: QemuVm | None = None
        if not existing_overlay:
            async with self._lock:
                if self.warm:
                    vm = self.warm.pop()
        if vm is not None:
            await vm.reset_workspace()
            vm.record.session_id = session_id
        else:
            async with self._lock:
                over_capacity = len(self.all_vms) >= self.settings.sandbox_max_vms
            if over_capacity:
                await self._evict_lru()
            vm = QemuVm(settings=self.settings, host=self.host, session_id=session_id)
            try:
                await vm.start()
            except SandboxBootError as exc:
                raise RpcError(ErrorCode.SANDBOX_ERROR, str(exc)) from exc
        async with self._lock:
            self.all_vms[vm.record.vm_id] = vm
            self.bindings[session_id] = SessionBinding(session_id=session_id, vm=vm)
        log.info(
            "session %s bound to %s (%s workspace)",
            session_id,
            vm.record.vm_id,
            "restored" if existing_overlay else "fresh",
        )
        asyncio.create_task(self._top_up_pool())  # noqa: RUF006 - fire and forget refill
        return vm

    async def release(self, session_id: str, *, stop_vm: bool = True) -> bool:
        async with self._lock:
            binding = self.bindings.pop(session_id, None)
        if binding is None:
            return False
        self.all_vms.pop(binding.vm.record.vm_id, None)
        if stop_vm:
            await binding.vm.stop()
        return True

    async def _evict_session(self, session_id: str) -> None:
        """Stop and forget the VM bound to a session (its overlay file survives)."""
        async with self._lock:
            binding = self.bindings.pop(session_id, None)
            if binding is not None:
                self.all_vms.pop(binding.vm.record.vm_id, None)
        if binding is not None:
            with contextlib.suppress(Exception):
                await binding.vm.stop(force=True)
        asyncio.create_task(self._top_up_pool())  # noqa: RUF006 - fire and forget refill

    # --------------------------------------------------------------- invoking
    async def invoke(self, params: SandboxInvokeParams) -> SandboxInvokeResult:
        started = time.monotonic()
        try:
            vm = await self.claim(params.session_id)
        except RpcError as exc:
            return SandboxInvokeResult(ok=False, error=exc.message, error_code=exc.label, duration_ms=_ms(started))

        binding = self.bindings.get(params.session_id)
        if binding is not None:
            binding.last_used = time.time()
            binding.invocations += 1

        try:
            if params.kind == "native":
                assert params.method
                result = await vm.call(params.method, params.params, timeout=params.timeout_s)
            else:
                assert params.tool is not None
                result = await self._run_python(vm, params.tool, params.params, params.timeout_s)
        except RpcError as exc:
            if exc.label in STALE_VM_CODES:
                # The VM is alive as a process but can never answer again (guest executor
                # restarted, port closed, ...).  Drop it so the next call binds a fresh
                # one; the session overlay stays on disk, so the workspace survives.
                log.warning(
                    "evicting %s for session %s after %s: %s",
                    vm.record.vm_id,
                    params.session_id,
                    exc.label,
                    exc.message,
                )
                await self._evict_session(params.session_id)
                return SandboxInvokeResult(
                    ok=False,
                    error=f"{exc.message} (the sandbox VM was recycled; retry the call)",
                    error_code=exc.label,
                    duration_ms=_ms(started),
                    vm_id=vm.record.vm_id,
                )
            return SandboxInvokeResult(
                ok=False,
                error=exc.message,
                error_code=exc.label,
                duration_ms=_ms(started),
                vm_id=vm.record.vm_id,
            )
        except Exception as exc:  # noqa: BLE001 - never leak a 500 to the agent loop
            return SandboxInvokeResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                error_code="internal_error",
                duration_ms=_ms(started),
                vm_id=vm.record.vm_id,
            )
        return SandboxInvokeResult(
            ok=True,
            result=result,
            duration_ms=_ms(started),
            vm_id=vm.record.vm_id,
        )

    async def _run_python(
        self,
        vm: QemuVm,
        tool: ToolPayload,
        args: dict,
        timeout_s: float | None,
    ) -> dict:
        timeout = float(timeout_s or tool.timeout_s)
        return await vm.call(
            "py.run",
            {
                "tool_name": tool.name,
                "version": tool.version,
                "sha256": tool.sha256,
                "source_b64": tool.source_b64,
                "args": args,
                "permissions": list(tool.permissions),
                "entrypoint": tool.entrypoint,
                "timeout_s": timeout,
            },
            # one extra second so the guest timeout fires before the transport timeout
            timeout=timeout + 5.0,
        )

    async def run_static_check(self, session_id: str, source: str, permissions: list[str], entrypoint: str = "run") -> dict:
        vm = await self.claim(session_id)
        return await vm.call(
            "py.check",
            {"source": source, "permissions": permissions, "entrypoint": entrypoint},
            timeout=30.0,
        )

    async def run_tool_tests(
        self,
        session_id: str,
        tool: ToolPayload,
        tests: list[dict],
        timeout_s: float,
    ) -> dict:
        vm = await self.claim(session_id)
        return await vm.call(
            "tool.test",
            {
                "tool_name": tool.name,
                "version": tool.version,
                "sha256": tool.sha256,
                "source_b64": tool.source_b64,
                "permissions": list(tool.permissions),
                "entrypoint": tool.entrypoint,
                "tests": tests,
                "timeout_s": timeout_s,
            },
            timeout=max(60.0, timeout_s * len(tests) + 15.0),
        )

    # ----------------------------------------------------------------- status
    async def status(self) -> PoolStatus:
        async with self._lock:
            vms = list(self.all_vms.values())
            warm = len(self.warm)
            active = len(self.bindings)
        entries: list[VmStatus] = []
        for vm in vms:
            entries.append(
                VmStatus(
                    vm_id=vm.record.vm_id,
                    session_id=vm.record.session_id,
                    state=vm.record.state,  # type: ignore[arg-type]
                    accel=vm.accel,
                    booted_at=(
                        time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(vm.record.booted_at))
                        if vm.record.booted_at
                        else None
                    ),
                    uptime_s=round(vm.uptime_s, 1),
                    commands_run=vm.record.commands_run,
                )
            )
        return PoolStatus(
            accel=self.accel[1] if len(self.accel) > 1 else "auto",
            pool_size=self.settings.sandbox_pool_size,
            warm=warm,
            active=active,
            vms=entries,
            image_problems=self.settings.sandbox_image_problems(),
        )

    # -------------------------------------------------------------- reaping
    async def _reap_loop(self) -> None:
        interval = max(30.0, min(300.0, self.settings.sandbox_idle_reap_s / 4))
        while True:
            try:
                await asyncio.sleep(interval)
                await self._reap_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the reaper must never die
                log.warning("sandbox reaper error: %s", exc)

    async def _reap_once(self) -> None:
        now = time.time()
        stale: list[str] = []
        for session_id, binding in list(self.bindings.items()):
            idle = now - binding.last_used
            age = now - binding.created_at
            if idle > self.settings.sandbox_idle_reap_s or age > self.settings.sandbox_session_ttl_s:
                stale.append(session_id)
        for session_id in stale:
            log.info("reaping idle sandbox for session %s", session_id)
            await self.release(session_id)
        for vm in list(self.warm):
            if vm.record.booted_at and now - vm.record.booted_at > self.settings.sandbox_session_ttl_s:
                async with self._lock:
                    if vm in self.warm:
                        self.warm.remove(vm)
                    self.all_vms.pop(vm.record.vm_id, None)
                await vm.destroy()
        await self._top_up_pool()

    async def acquire(self, session_id: str) -> SandboxHandle:
        existing = self.bindings.get(session_id)
        vm = await self.claim(session_id)
        return SandboxHandle(
            session_id=session_id,
            vm_id=vm.record.vm_id,
            state="ready" if vm.alive else "booting",
            reused=existing is not None,
        )


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
