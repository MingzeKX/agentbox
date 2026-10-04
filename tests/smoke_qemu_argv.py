"""Validate the real QEMU invocation on this host, without a bootable image.

Two things are checked:

1. ``qemu-img create -b base -F qcow2 overlay.qcow2`` works (session overlays).
2. the argv produced by :meth:`QemuVm.build_argv` is accepted by the installed
   QEMU: option parsing happens before any file/kernel validation, so an
   "invalid option" error means the command line is wrong, while "could not
   open" / "invalid kernel" only means the dummy images are not real images.
"""

from __future__ import annotations

import subprocess
import sys

from agent.config import project_root, settings
from agent.control.host.base import build_backend
from agent.control.vm import QemuVm


def main() -> int:
    problems: list[str] = []
    print(f"project root : {project_root()}")
    print(f"sandbox dir  : {settings.sandbox_root}")

    try:
        qemu_system = settings.qemu_system
        qemu_img = settings.qemu_img
    except FileNotFoundError as exc:
        print(f"FAIL {exc}")
        return 1
    print(f"qemu-system  : {qemu_system}")
    print(f"qemu-img     : {qemu_img}")

    acceleration = build_backend(settings).accel_args()
    print(f"accelerator  : {' '.join(acceleration)}")

    # ------------------------------------------------------------------ overlay
    scratch = settings.sandbox_root / "smoke"
    scratch.mkdir(parents=True, exist_ok=True)
    base = scratch / "base.qcow2"
    overlay = scratch / "overlay.qcow2"
    overlay.unlink(missing_ok=True)
    subprocess.run([str(qemu_img), "create", "-f", "qcow2", str(base), "8M"], check=True, capture_output=True)
    result = subprocess.run(
        [
            str(qemu_img),
            "create",
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            "-b",
            str(base),
            str(overlay),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        problems.append(f"qemu-img overlay failed: {result.stderr.strip()}")
    else:
        info = subprocess.run(
            [str(qemu_img), "info", "--output=json", str(overlay)], capture_output=True, text=True
        )
        import json

        backing = json.loads(info.stdout or "{}").get("backing-filename")
        print(f"overlay       : created, backing={backing}")
        if not backing:
            problems.append("overlay has no backing file")

    # -------------------------------------------------------------------- argv
    dummy_kernel = scratch / "vmlinuz"
    dummy_initrd = scratch / "initrd.img"
    dummy_rootfs = scratch / "rootfs.img"
    dummy_workspace = scratch / "workspace-blank.qcow2"
    dummy_kernel.write_bytes(b"\x00" * 4096)
    dummy_initrd.write_bytes(b"\x00" * 4096)
    dummy_rootfs.write_bytes(b"\x00" * 8192)
    subprocess.run(
        [str(qemu_img), "create", "-f", "qcow2", str(dummy_workspace), "8M"], check=True, capture_output=True
    )

    original = (settings.base_kernel, settings.base_initrd, settings.base_rootfs, settings.blank_workspace)
    replacements = {
        str(original[0]): str(dummy_kernel),
        str(original[1]): str(dummy_initrd),
        str(original[2]): str(dummy_rootfs),
        str(original[3]): str(dummy_workspace),
    }
    try:
        vm = QemuVm(settings=settings, host=build_backend(settings), session_id="smoke")
        # point the session overlay at a dummy backing file so QEMU can open it
        vm.session_dir.mkdir(parents=True, exist_ok=True)
        vm.overlay.unlink(missing_ok=True)
        subprocess.run(
            [
                str(qemu_img),
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(dummy_workspace),
                str(vm.overlay),
            ],
            check=True,
            capture_output=True,
        )
        argv = vm.build_argv(port=1)
        for index, argument in enumerate(argv):
            for real, dummy in replacements.items():
                if real and real in argument:
                    argument = argument.replace(real, dummy)
            argv[index] = argument
        argv[argv.index("-chardev") + 1] = "socket,id=vs0,host=127.0.0.1,port=1,server=off,nodelay=on"
        print("qemu argv    :", " ".join(argv))
        try:
            probe = subprocess.run(argv, capture_output=True, text=True, timeout=25)
            stderr = probe.stderr or ""
        except subprocess.TimeoutExpired as exc:
            stderr = (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    finally:
        pass

    bad_markers = ("invalid option", "Unknown option", "invalid char", "unrecognized")
    lower = stderr.lower()
    hits = [marker for marker in bad_markers if marker.lower() in lower]
    if hits:
        problems.append(f"qemu rejected the command line ({hits}): {stderr.strip()[:400]}")
    else:
        print(f"qemu parsed : argv accepted (exit reason: {stderr.strip().splitlines()[:1] or 'no output'})")

    print()
    if problems:
        for problem in problems:
            print("FAIL", problem)
        return 1
    print("QEMU invocation smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
