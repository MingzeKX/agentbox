"""QEMU process management: one VM per session, one overlay disk per session.

Hardening applied to every VM:

============================  ==================================================
read-only root                kernel ``rootflags=ro`` + init remounts ``/`` ro
no network                    ``-nic none`` (no NIC at all) + guest nftables
single writable disk          ``/workspace`` on the second virtio disk
no host shares               no 9p/virtiofs/block passthrough to host paths
no display, no usb, no audio  only virtio-serial + virtio-rng + two virtio disks
resource caps                 host Job Object / cgroup v2, ``-m``/``-smp`` caps
transport                     TCP loopback chardev; the control plane is the
                              *server* so there is no port guessing
authentication                per-VM random token over fw_cfg + HMAC proof
============================  ==================================================
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import secrets
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from agent.config import Settings
from agent.control.client import SandboxClient
from agent.control.host.base import HostBackend, HostLimits, accel_argv, detect_accel
from agent.models.protocol import ErrorCode, RpcError

log = logging.getLogger(__name__)

KERNEL_APPEND = (
    "console=ttyS0 root=/dev/vda ro rootflags=ro rootfstype=ext4 rootwait "
    "init=/sbin/agent-init panic=1 oops=panic quiet net.ifnames=0 random.trust_cpu=on"
)
WORKSPACE_DISK_BYTES = 512 * 1024 * 1024


class SandboxBootError(RuntimeError):
    pass


def _base_fingerprint(base: Path) -> str:
    """Cheap identity of the immutable base image (size + mtime + first/last KiB)."""
    info = base.stat()
    with base.open("rb") as handle:
        head = handle.read(1024)
        handle.seek(max(0, info.st_size - 1024))
        tail = handle.read(1024)
    digest = hashlib.sha256()
    digest.update(f"{info.st_size}:{int(info.st_mtime)}".encode())
    digest.update(head)
    digest.update(tail)
    return digest.hexdigest()


def create_overlay(base: Path, target: Path) -> None:
    """Create a qcow2 overlay backed by an immutable base image (idempotent).

    If the base image changed since the overlay was made (a rebuilt sandbox image),
    the overlay is stale: its unchanged clusters now read the *new* base, which can
    produce I/O errors inside the guest.  Invalidate it in that case.
    """
    from agent.config import settings as global_settings

    stamp = target.parent / ".base-fingerprint"
    fingerprint = _base_fingerprint(base) if base.exists() else "missing"
    if target.exists():
        recorded = stamp.read_text(encoding="utf-8").strip() if stamp.exists() else ""
        if recorded == fingerprint:
            return
        log.warning(
            "base image changed (%s): discarding stale overlay %s",
            base.name,
            target,
        )
        target.unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    qemu_img = global_settings.qemu_img
    result = subprocess.run(  # noqa: S603 - fixed binary, explicit argv
        [
            str(qemu_img),
            "create",
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            "-b",
            str(base),
            str(target),
        ],
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise SandboxBootError(
            f"qemu-img create failed for {target.name}: {result.stderr.decode(errors='replace').strip()}"
        )
    # Remember which base this overlay belongs to, so replacing the sandbox image
    # invalidates it instead of producing confusing I/O errors in the guest.
    with contextlib.suppress(OSError):
        stamp.write_text(fingerprint, encoding="utf-8")


@dataclass
class VmRecord:
    vm_id: str
    session_id: str | None = None
    workspace_key: str = ""
    state: str = "booting"
    booted_at: float | None = None
    commands_run: int = 0
    cgroup_events: dict[str, int] = field(default_factory=dict)


class QemuVm:
    """Owns one QEMU process, its overlay disk and the RPC client."""

    def __init__(
        self,
        *,
        settings: Settings,
        host: HostBackend,
        vm_id: str | None = None,
        session_id: str | None = None,
        accel: str | None = None,
    ) -> None:
        self.settings = settings
        self.host = host
        self.record = VmRecord(
            vm_id=vm_id or f"vm-{uuid.uuid4().hex[:10]}",
            session_id=session_id,
            workspace_key=session_id or (vm_id or ""),
        )
        if not self.record.workspace_key:
            self.record.workspace_key = self.record.vm_id
        self.accel = accel or detect_accel(settings.sandbox_accel)
        self.token = settings.rpc_token or secrets.token_hex(32)
        self.process: asyncio.subprocess.Process | None = None
        self.client: SandboxClient | None = None
        self.confinement = None
        self.listener: asyncio.AbstractServer | None = None
        self._connection: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        self._connected = asyncio.Event()
        self._stderr_handle = None

    # ------------------------------------------------------------------ paths
    @property
    def session_dir(self) -> Path:
        return self.settings.sessions_dir / self.record.workspace_key

    @property
    def overlay(self) -> Path:
        return self.session_dir / "workspace.qcow2"

    @property
    def console_log(self) -> Path:
        return self.settings.console_dir / f"{self.record.vm_id}.log"

    @property
    def qemu_log(self) -> Path:
        return self.settings.console_dir / f"{self.record.vm_id}.qemu.log"

    # --------------------------------------------------------------- builders
    def build_argv(self, port: int) -> list[str]:
        s = self.settings
        argv = [
            str(s.qemu_system),
            "-machine",
            "q35",
            *accel_argv(self.accel, self.settings.qemu_cpu),
            "-smp",
            str(s.sandbox_vm_cpus),
            "-m",
            str(s.sandbox_vm_memory_mb),
            "-kernel",
            str(s.base_kernel),
            "-initrd",
            str(s.base_initrd),
            "-append",
            KERNEL_APPEND,
            "-drive",
            f"file={s.base_rootfs},if=virtio,format=raw,readonly=on",
            "-drive",
            f"file={self.overlay},if=virtio,format=qcow2,discard=unmap",
            "-chardev",
            f"socket,id=vs0,host=127.0.0.1,port={port},server=off,nodelay=on",
            "-device",
            "virtio-serial-pci",
            "-device",
            "virtserialport,chardev=vs0,name=agent.rpc",
            "-device",
            "virtio-rng-pci",
            "-fw_cfg",
            f"name=opt/agent/token,string={self.token}",
            "-fw_cfg",
            f"name=opt/agent/vm_id,string={self.record.vm_id}",
            "-fw_cfg",
            f"name=opt/agent/net_mode,string={self.settings.sandbox_net_mode}",
            "-nic",
            # 'full' attaches a slirp user-mode NIC (guest 10.0.2.15/24, gw 10.0.2.2,
            # DNS 10.0.2.3).  'off' keeps the sandbox with no NIC whatsoever.
            (
                "user,model=virtio-net-pci"
                if getattr(self.settings, "sandbox_net_mode", "off") == "full"
                else "none"
            ),
            "-display",
            "none",
            "-serial",
            f"file:{self.console_log}",
            "-no-reboot",
            "-rtc",
            "base=utc",
        ]
        return argv

    # ---------------------------------------------------------------- startup
    async def start(self) -> None:
        s = self.settings
        problems = s.sandbox_image_problems()
        if problems:
            raise SandboxBootError("; ".join(problems) + " -- run: python -m agent.cli image build")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.console_log.parent.mkdir(parents=True, exist_ok=True)
        create_overlay(s.blank_workspace, self.overlay)

        self.listener = await asyncio.start_server(self._on_connect, "127.0.0.1", 0)
        port = self.listener.sockets[0].getsockname()[1]
        argv = self.build_argv(port)
        self.record.state = "booting"
        log.info("starting %s accel=%s session=%s", self.record.vm_id, self.accel, self.record.session_id)
        log.debug("qemu argv: %s", " ".join(argv))

        self._stderr_handle = open(self.qemu_log, "ab")  # noqa: SIM115 - closed in stop()
        self.process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=self._stderr_handle,
            stdin=asyncio.subprocess.DEVNULL,
        )
        self.confinement = self.host.confine(
            self.process.pid or 0,
            HostLimits(
                memory_bytes=s.sandbox_job_memory_bytes,
                max_processes=s.sandbox_job_max_processes,
                cpus=s.sandbox_vm_cpus,
            ),
        )
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=s.sandbox_boot_timeout_s)
            reader, writer = self._connection  # type: ignore[misc]
            self.client = SandboxClient(
                reader,
                writer,
                token=self.token,
                vm_id=self.record.vm_id,
                default_timeout=max(60.0, s.exec_default_timeout_s * 2),
            )
            await self.client.start()
            await self._wait_for_guest()
        except Exception as exc:
            detail = self._failure_detail()
            await self.stop(force=True)
            raise SandboxBootError(f"{self.record.vm_id} failed to boot: {exc}{detail}") from exc

        self.record.state = "ready"
        self.record.booted_at = time.time()
        log.info("%s ready (pid=%s)", self.record.vm_id, self.process.pid)

    async def _wait_for_guest(self) -> None:
        """The chardev connects before the guest is up; poll until it answers."""
        deadline = time.monotonic() + self.settings.sandbox_boot_timeout_s
        last: Exception | None = None
        while time.monotonic() < deadline:
            if self.process is not None and self.process.returncode is not None:
                raise SandboxBootError(f"qemu exited with {self.process.returncode}")
            try:
                assert self.client is not None
                hello = await self.client.hello(timeout=10.0)
                log.info(
                    "%s guest up: kernel=%s python=%s executor_uid=%s",
                    self.record.vm_id,
                    hello.kernel,
                    hello.python,
                    hello.uid,
                )
                return
            except RpcError as exc:
                last = exc
                await asyncio.sleep(1.0)
        raise SandboxBootError(f"guest never answered sys.hello ({last})")

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._connection is not None:
            log.debug("ignoring extra chardev connection for %s", self.record.vm_id)
            writer.close()
            return
        self._connection = (reader, writer)
        self._connected.set()

    def _failure_detail(self) -> str:
        lines: list[str] = []
        for path in (self.console_log, self.qemu_log):
            try:
                tail = path.read_text(errors="replace").strip().splitlines()[-12:]
            except OSError:
                continue
            if tail:
                lines.append(f"\n--- {path.name} ---\n" + "\n".join(tail))
        return "".join(lines)

    # ------------------------------------------------------------------- stop
    async def stop(self, *, force: bool = False) -> None:
        self.record.state = "stopped"
        if self.client is not None and not force:
            with contextlib.suppress(Exception):
                await self.client.shutdown()
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.close()
            self.client = None
        if self.listener is not None:
            self.listener.close()
            with contextlib.suppress(Exception):
                await self.listener.wait_closed()
            self.listener = None
        if self.process is not None and self.process.returncode is None:
            try:
                await asyncio.wait_for(self.process.wait(), timeout=15.0 if not force else 1.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                with contextlib.suppress(Exception):
                    await self.process.wait()
        if self.confinement is not None:
            self.record.cgroup_events = self.confinement.usage()
            self.confinement.release()
            self.confinement = None
        if self._stderr_handle is not None:
            with contextlib.suppress(Exception):
                self._stderr_handle.close()
            self._stderr_handle = None
        if self._connection is not None:
            with contextlib.suppress(Exception):
                self._connection[1].close()
            self._connection = None
        log.info("%s stopped", self.record.vm_id)

    async def destroy(self, *, remove_workspace: bool = False) -> None:
        await self.stop(force=True)
        if remove_workspace:
            shutil.rmtree(self.session_dir, ignore_errors=True)

    # ------------------------------------------------------------------- calls
    async def call(self, method: str, params: dict | None = None, timeout: float | None = None):
        if self.client is None:
            raise RpcError(ErrorCode.UNAVAILABLE, f"sandbox {self.record.vm_id} is not running")
        result = await self.client.call(method, params, timeout=timeout)
        if method in {"exec.run", "py.run", "tool.test"}:
            self.record.commands_run += 1
        return result

    async def reset_workspace(self) -> None:
        with contextlib.suppress(RpcError):
            await self.call("sandbox.reset", {"keep_tools": True}, timeout=30.0)

    async def metrics(self) -> dict:
        try:
            return await self.call("sandbox.info", {}, timeout=15.0)
        except RpcError as exc:
            return {"error": exc.message}

    @property
    def uptime_s(self) -> float:
        return 0.0 if self.record.booted_at is None else time.time() - self.record.booted_at

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None and self.record.state == "ready"
