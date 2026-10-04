"""Guest-side network bring-up, driven by the operator's sandbox network switch.

Design
------
The sandbox normally runs with ``-nic none``: no NIC at all, so nothing to configure and
nothing to leak.  With ``AGENT_SANDBOX_NET_MODE=full`` QEMU attaches a user-mode (slirp)
NIC and the host passes ``opt/agent/net_mode=full`` through fw_cfg; the guest then:

* brings ``eth0`` up with slirp's fixed addressing (10.0.2.15/24, gateway 10.0.2.2,
  DNS 10.0.2.3) -- static, because the image deliberately has no DHCP client,
* writes ``/etc/resolv.conf`` -- which is a **symlink to /run/agent/resolv.conf**,
  because the root filesystem is mounted read-only,
* applies an egress-permissive, ingress-closed nftables ruleset (defence in depth: no
  other guest and no host service can reach into the sandbox).

Everything is a pure plan first (:func:`plan`) so it can be unit-tested without a VM.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field

try:  # repository layout
    from agent.sandbox import policy
except ImportError:  # pragma: no cover - guest layout (modules are installed flat)
    import policy  # type: ignore[no-redef]

SLIRP_GUEST_IP = "10.0.2.15/24"
SLIRP_GATEWAY = "10.0.2.2"
SLIRP_DNS = "10.0.2.3"
IFACE = "eth0"
RESOLV = "/run/agent/resolv.conf"
#: lets the unprivileged sandbox user create ICMP sockets (ping)
PING_GROUP_RANGE = "0 2147483647"
#: apt writes here; both sit on the read-only root, so they get a tmpfs
APT_LISTS = "/var/lib/apt/lists"
APT_CACHE = "/var/cache/apt/archives"


@dataclass
class NetPlan:
    mode: str
    commands: list[list[str]] = field(default_factory=list)
    resolv_conf: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.mode == "full"


def plan(mode: str) -> NetPlan:
    """Pure description of what bring-up does for ``mode`` (``off`` or ``full``)."""
    mode = (mode or "off").strip().lower()
    if mode not in {"off", "full"}:
        mode = "off"
    if mode == "off":
        return NetPlan(mode="off", notes=["no NIC is attached; nothing to configure"])
    return NetPlan(
        mode="full",
        commands=[
            ["ip", "link", "set", IFACE, "up"],
            ["ip", "addr", "add", SLIRP_GUEST_IP, "dev", IFACE],
            ["ip", "route", "add", "default", "via", SLIRP_GATEWAY, "dev", IFACE],
            # unprivileged ping: the sandbox user has no CAP_NET_RAW, and without this
            # range the kernel refuses ICMP sockets outright
            ["sysctl", "-w", f"net.ipv4.ping_group_range={PING_GROUP_RANGE}"],
            # apt state must be writable even though / is read-only
            ["mount", "-t", "tmpfs", "-o", "mode=0755,size=64m", "tmpfs", APT_LISTS],
            ["mount", "-t", "tmpfs", "-o", "mode=0755,size=256m", "tmpfs", APT_CACHE],
            # every command runs as uid 1000, so the fresh tmpfs trees must belong to it
            # (apt also wants <dir>/partial to exist); apt-chown marker keeps this
            # idempotent in the plan
            [
                "bash",
                "-c",
                f"mkdir -p {APT_LISTS}/partial {APT_CACHE}/partial && chown -R 1000:1000 {APT_LISTS} {APT_CACHE}",
            ],
            # ingress closed, egress (and replies to our own flows) allowed
            [
                "nft",
                "-f",
                "/etc/agent/nftables-net.conf",
            ],
        ],
        resolv_conf=f"nameserver {SLIRP_DNS}\n",
        notes=[
            "slirp static addressing (no DHCP client in the image)",
            "outbound only: inbound from the host side is dropped",
            "ping works via net.ipv4.ping_group_range (no CAP_NET_RAW needed)",
            "apt update works: lists/cache are tmpfs; apt install needs a writable root",
        ],
    )


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode, output.strip()


def apply(mode: str) -> dict:
    """Execute the plan.  Returns a report dict (never raises)."""
    net = plan(mode)
    report: dict = {"mode": net.mode, "enabled": net.enabled, "notes": net.notes, "steps": [], "ok": True}
    if not net.enabled:
        return report

    if not os.path.isdir(f"/sys/class/net/{IFACE}"):
        report["ok"] = False
        report["error"] = f"{IFACE} is missing although net mode is 'full' (host did not attach a NIC?)"
        return report

    os.makedirs("/run/agent", exist_ok=True)
    for path in (APT_LISTS, APT_CACHE):
        os.makedirs(path, exist_ok=True)
    if net.resolv_conf:
        try:
            with open(RESOLV, "w", encoding="utf-8") as handle:
                handle.write(net.resolv_conf)
        except OSError as exc:
            report["steps"].append({"step": f"write {RESOLV}", "returncode": 1, "output": str(exc)})
        else:
            report["steps"].append({"step": f"write {RESOLV}", "returncode": 0, "output": net.resolv_conf.strip()})

    for argv in net.commands:
        code, output = _run(argv)
        report["steps"].append({"step": " ".join(argv), "returncode": code, "output": output[:300]})
        if code != 0 and "File exists" not in output:
            report["ok"] = False
            report["error"] = f"{' '.join(argv)} failed: {output[:200]}"
    return report


def log_report(report: dict) -> None:
    print(f"network: mode={report['mode']} enabled={report['enabled']}", flush=True)
    for step in report.get("steps", []):
        print(f"  [{step['returncode']}] {step['step']}: {step['output']}", flush=True)
    if report.get("error"):
        print(f"  ERROR: {report['error']}", file=sys.stderr, flush=True)


def mode_from_host() -> str:
    """What the host asked for (fw_cfg), defaulting to off."""
    return policy.read_fw_cfg("net_mode", "off") or "off"
