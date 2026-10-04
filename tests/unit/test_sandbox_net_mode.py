"""The sandbox network switch: QEMU side, guest plan, and the guest import layout.

The sandbox is NIC-less by default.  ``AGENT_SANDBOX_NET_MODE=full`` is the operator's
explicit opt-in and it has to hold together in three places: the QEMU argv, the fw_cfg
flag the guest reads, and the guest-side bring-up plan.  A regression in any of them
either silently keeps the sandbox offline or silently gives it a network it was not
asked for, so all three are pinned here.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from agent.config import Settings
from agent.control.host.base import NullBackend
from agent.control.vm import QemuVm
from agent.sandbox import network

REPO = Path(__file__).resolve().parents[2]


def argv_for(settings: Settings) -> list[str]:
    return QemuVm(settings=settings, host=NullBackend(), session_id="s-net").build_argv(1)


def test_default_is_off_and_the_guest_is_told_so(settings_factory):
    settings = settings_factory(sandbox_net_mode="off")
    assert settings.sandbox_net_mode == "off"
    argv = argv_for(settings)
    assert argv[argv.index("-nic") + 1] == "none"
    assert "name=opt/agent/net_mode,string=off" in argv


def test_full_mode_attaches_slirp_and_flags_the_guest(settings_factory):
    argv = argv_for(settings_factory(sandbox_net_mode="full"))
    assert argv[argv.index("-nic") + 1] == "user,model=virtio-net-pci"
    assert "name=opt/agent/net_mode,string=full" in argv
    joined = " ".join(argv).lower()
    # a NIC is the only concession: still no host share and no inbound forwarding
    assert "hostfwd" not in joined
    assert "-virtfs" not in joined and "-fsdev" not in joined


# ------------------------------------------------------------------ guest plan


def test_off_mode_plans_nothing():
    plan = network.plan("off")
    assert plan.enabled is False
    assert plan.commands == []
    assert plan.resolv_conf == ""


def test_full_mode_plans_slirp_addressing():
    plan = network.plan("full")
    assert plan.enabled is True
    joined = [" ".join(command) for command in plan.commands]
    assert "ip link set eth0 up" in joined
    assert f"ip addr add {network.SLIRP_GUEST_IP} dev eth0" in joined
    assert f"ip route add default via {network.SLIRP_GATEWAY} dev eth0" in joined
    assert "nft -f /etc/agent/nftables-net.conf" in joined
    # unprivileged ping (uid 1000 has no CAP_NET_RAW) and writable apt state
    assert f"sysctl -w net.ipv4.ping_group_range={network.PING_GROUP_RANGE}" in joined
    assert f"mount -t tmpfs -o mode=0755,size=64m tmpfs {network.APT_LISTS}" in joined
    assert f"mount -t tmpfs -o mode=0755,size=256m tmpfs {network.APT_CACHE}" in joined
    assert any("chown -R 1000:1000" in command and network.APT_LISTS in command for command in joined)
    assert plan.resolv_conf == f"nameserver {network.SLIRP_DNS}\n"


def test_unknown_modes_fall_back_to_off():
    assert network.plan("yes please").enabled is False
    assert network.plan("").enabled is False
    assert network.plan(None).enabled is False  # type: ignore[arg-type]


def test_apply_is_a_noop_when_off(tmp_path, monkeypatch):
    monkeypatch.setattr(network, "RESOLV", str(tmp_path / "resolv.conf"))
    report = network.apply("off")
    assert report["ok"] is True
    assert report["steps"] == []
    assert not (tmp_path / "resolv.conf").exists()


def test_apply_reports_a_missing_interface(monkeypatch):
    """If the host said 'full' but no NIC appeared, say so instead of pretending."""
    monkeypatch.setattr(network.os.path, "isdir", lambda path: False)
    report = network.apply("full")
    assert report["ok"] is False
    assert "eth0" in report["error"]


# ------------------------------------------------------- guest import layout


def test_module_can_be_imported_flat_like_in_the_guest_image(tmp_path):
    """The guest has no ``agent`` package: modules live flat under /usr/lib/agent.

    Every sandbox VM died at boot with "ModuleNotFoundError: No module named 'agent'"
    when network.py used only the package-style import, so the flat layout is pinned.
    """
    for source in (REPO / "src" / "agent" / "sandbox").glob("*.py"):
        shutil.copy(source, tmp_path / source.name)
    script = (
        f"import sys; sys.path.insert(0, r'{tmp_path}');"
        "import network; assert network.plan('full').enabled; print('flat-ok')"
    )
    import subprocess

    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)  # noqa: S603
    assert result.returncode == 0, result.stderr
    assert "flat-ok" in result.stdout


def test_guest_files_are_all_copied_into_the_image():
    """The image copies ``sandbox/*.py``: a new guest module must not be left behind."""
    recipe = (REPO / "deploy" / "sandbox" / "build-sandbox-image.sh").read_text(encoding="utf-8")
    assert '"${GUEST_SRC}"/*.py' in recipe
    assert (REPO / "src" / "agent" / "sandbox" / "network.py").is_file()
