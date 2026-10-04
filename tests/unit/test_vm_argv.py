"""Structural checks on the QEMU command line.

The sandbox guarantees that live in the *host* command line (no network device,
read-only root disk, no host directory shares, per-VM secret injection) are
regression tested here, because they are easy to break by accident and expensive
to notice later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.config import project_root
from agent.control.host.base import NullBackend, accel_argv, detect_accel
from agent.control.vm import KERNEL_APPEND, QemuVm


@pytest.fixture
def vm(settings_factory) -> QemuVm:
    settings = settings_factory()
    return QemuVm(
        settings=settings,
        host=NullBackend(),
        vm_id="vm-argv-test",
        session_id="s-argv",
        accel="tcg",
    )


def argv_of(vm: QemuVm, port: int = 41234) -> list[str]:
    return vm.build_argv(port)


def test_no_network_device_by_default(settings_factory):
    """The sandbox is isolated unless the operator explicitly asks for internet.

    ``AGENT_SANDBOX_NET_MODE`` (default ``off``) is the single switch that changes this,
    and the host tells the guest which mode it is in through fw_cfg so the guest knows
    whether to configure eth0.  Built with an explicit "off" so the check does not
    depend on whatever this machine's .env happens to say.
    """
    vm = QemuVm(settings=settings_factory(sandbox_net_mode="off"), host=NullBackend(), session_id="s1")
    argv = argv_of(vm)
    assert "-nic" in argv
    assert argv[argv.index("-nic") + 1] == "none"
    joined = " ".join(argv).lower()
    for forbidden in ("-netdev", "e1000", "rtl8139", "virtio-net", "slirp", "hostfwd", "tap"):
        assert forbidden not in joined, f"{forbidden} would give the sandbox a network"
    assert "name=opt/agent/net_mode,string=off" in argv


def test_full_network_mode_attaches_a_slirp_nic(settings_factory):
    """Opt-in only: 'full' gives the guest a real NIC -- and nothing else."""
    settings = settings_factory(sandbox_net_mode="full")
    vm = QemuVm(settings=settings, host=NullBackend(), accel="whpx", session_id="s1")
    argv = vm.build_argv(1)
    joined = " ".join(argv).lower()
    assert argv[argv.index("-nic") + 1] == "user,model=virtio-net-pci"
    assert "name=opt/agent/net_mode,string=full" in argv
    # a NIC is the *only* thing that changes: no host directory, no port forwarding
    assert "hostfwd" not in joined and "-virtfs" not in joined and "-fsdev" not in joined


def test_root_disk_is_read_only_and_the_workspace_disk_is_per_session(vm):
    argv = argv_of(vm)
    drives = [argv[index + 1] for index, item in enumerate(argv) if item == "-drive"]
    assert len(drives) == 2
    root, workspace = drives
    assert "readonly=on" in root and "format=raw" in root
    assert "readonly" not in workspace
    assert "s-argv" in workspace
    assert "workspace.qcow2" in workspace
    assert "if=virtio" in workspace


def test_no_host_directory_is_shared_with_the_guest(vm):
    argv = argv_of(vm)
    options = {item for item in argv if item.startswith("-")}
    for forbidden in ("-virtfs", "-fsdev", "-hda", "-hdb", "-snapshot", "-device"):
        if forbidden == "-device":
            continue
        assert forbidden not in options, f"{forbidden} would expose host resources"
    devices = [argv[index + 1] for index, item in enumerate(argv) if item == "-device"]
    for device in devices:
        assert "9p" not in device and "virtiofs" not in device
    assert "mount_tag=" not in " ".join(argv)
    assert "-virtfs" not in argv and "-fsdev" not in argv


def test_no_display_usb_audio_or_snapshot_devices(vm):
    argv = [item.lower() for item in argv_of(vm)]
    assert "-display" in argv and argv[argv.index("-display") + 1] == "none"
    for forbidden in ("-usb", "-device usb", "-audiodev", "-soundhw", "-snapshot", "-monitor"):
        assert forbidden not in " ".join(argv)


def test_vm_cannot_reboot_itself_into_a_different_configuration(vm):
    argv = argv_of(vm)
    assert "-no-reboot" in argv


def test_kernel_command_line_enforces_a_read_only_root_and_our_init(vm):
    argv = argv_of(vm)
    append = argv[argv.index("-append") + 1]
    assert "ro" in append.split()
    assert "rootflags=ro" in append
    assert "init=/sbin/agent-init" in append
    assert "console=ttyS0" in append
    assert append == KERNEL_APPEND


def test_the_rpc_token_never_reaches_the_kernel_command_line(vm):
    argv = argv_of(vm)
    append = argv[argv.index("-append") + 1]
    assert vm.token not in append, "the token must travel over fw_cfg, not /proc/cmdline"
    fw_cfg = [argv[index + 1] for index, item in enumerate(argv) if item == "-fw_cfg"]
    assert any(entry == f"name=opt/agent/token,string={vm.token}" for entry in fw_cfg)
    assert any(entry.startswith("name=opt/agent/vm_id,string=") for entry in fw_cfg)


def test_tokens_are_unique_per_vm(settings_factory):
    first = QemuVm(settings=settings_factory(), host=NullBackend(), accel="tcg")
    second = QemuVm(settings=settings_factory(), host=NullBackend(), accel="tcg")
    assert first.token != second.token
    assert len(first.token) == 64


def test_virtio_serial_carries_the_rpc_channel(vm):
    argv = argv_of(vm)
    chardev = argv[argv.index("-chardev") + 1]
    assert "socket" in chardev and "server=off" in chardev and "port=41234" in chardev
    assert "host=127.0.0.1" in chardev, "the RPC socket must stay on loopback"
    devices = [argv[index + 1] for index, item in enumerate(argv) if item == "-device"]
    assert "virtio-serial-pci" in devices
    assert any("name=agent.rpc" in device for device in devices)


def test_resource_caps_are_passed_to_qemu(settings_factory):
    settings = settings_factory()
    vm = QemuVm(settings=settings, host=NullBackend(), accel="tcg")
    argv = vm.build_argv(1)
    assert argv[argv.index("-m") + 1] == str(settings.sandbox_vm_memory_mb)
    assert argv[argv.index("-smp") + 1] == str(settings.sandbox_vm_cpus)


def test_console_output_is_redirected_to_a_file(vm):
    argv = argv_of(vm)
    serial = argv[argv.index("-serial") + 1]
    assert serial.startswith("file:")
    assert "vm-argv-test.log" in serial
    assert Path(serial[len("file:") :]).parent == vm.settings.console_dir


@pytest.mark.parametrize(
    ("accel", "expected"),
    [
        ("kvm", ["-accel", "kvm", "-cpu", "host"]),
        # WHPX on Windows dies with "-cpu host" and "-cpu max" (Unexpected VP exit
        # code 4), so the conservative qemu64 model is the default there.
        ("whpx", ["-accel", "whpx", "-cpu", "qemu64"]),
        ("tcg", ["-accel", "tcg,thread=multi", "-cpu", "max"]),
    ],
)
def test_accelerator_arguments(accel: str, expected: list[str]):
    assert accel_argv(accel) == expected


def test_cpu_model_can_be_overridden():
    assert accel_argv("whpx", "Nehalem") == ["-accel", "whpx", "-cpu", "Nehalem"]
    assert accel_argv("kvm", "Skylake-Client") == ["-accel", "kvm", "-cpu", "Skylake-Client"]


def test_settings_cpu_override_reaches_the_command_line(settings_factory):
    settings = settings_factory(sandbox_cpu="Nehalem")
    vm = QemuVm(settings=settings, host=NullBackend(), accel="whpx", session_id="s1")
    argv = vm.build_argv(1)
    assert argv[argv.index("-cpu") + 1] == "Nehalem"

    default = QemuVm(settings=settings_factory(), host=NullBackend(), accel="whpx", session_id="s1")
    argv = default.build_argv(1)
    assert argv[argv.index("-cpu") + 1] == "Nehalem"


def test_accelerator_detection_is_sane():
    assert detect_accel("kvm") == "kvm"
    assert detect_accel("whpx") == "whpx"
    assert detect_accel("auto") in {"kvm", "whpx", "tcg"}


def test_blank_workspace_backs_the_session_overlay(settings_factory):
    settings = settings_factory()
    vm = QemuVm(settings=settings, host=NullBackend(), accel="tcg", session_id="s-argv", vm_id="vm-argv-test")
    assert vm.overlay == settings.sessions_dir / "s-argv" / "workspace.qcow2"
    assert vm.session_dir.parent == settings.sessions_dir


def test_a_warm_vm_uses_its_own_id_until_it_is_claimed(settings_factory):
    settings = settings_factory()
    warm = QemuVm(settings=settings, host=NullBackend(), accel="tcg", vm_id="vm-warm-1")
    assert warm.overlay == settings.sessions_dir / "vm-warm-1" / "workspace.qcow2"


def test_image_problems_are_reported_when_files_are_missing(settings_factory, tmp_path: Path):
    settings = settings_factory(sandbox_dir=tmp_path / "empty-sandbox")
    problems = settings.sandbox_image_problems()
    assert len(problems) == 1
    assert "rootfs.img" in problems[0]
    assert settings.image_ready() is False


def test_qemu_lookup_reports_a_clear_error(settings_factory):
    settings = settings_factory(qemu_dir=project_root() / "does-not-exist")
    with pytest.raises(FileNotFoundError) as excinfo:
        settings.find_qemu("qemu-not-a-real-binary")
    assert "AGENT_QEMU_DIR" in str(excinfo.value)


def test_qemu_lookup_finds_the_bundled_windows_build(settings_factory):
    settings = settings_factory()
    found = settings.qemu_system
    assert found.is_file()
    assert found.name.startswith("qemu-system-x86_64")
