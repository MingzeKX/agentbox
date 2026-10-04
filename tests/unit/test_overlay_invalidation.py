"""Overlay invalidation when the sandbox image is rebuilt.

A session overlay is a qcow2 file backed by the *immutable* blank workspace. If the
sandbox image is rebuilt while overlays exist, the unchanged clusters of an old
overlay start reading the new backing file, which showed up as
`OSError: [Errno 5] Input/output error` inside the guest. The overlay must therefore
be tied to a fingerprint of its base image.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _fake_qemu_img(monkeypatch):
    def fake_run(argv, *args, **kwargs):
        Path(argv[-1]).write_bytes(b"overlay")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_overlay_is_reused_for_an_unchanged_base(tmp_path: Path, monkeypatch):
    from agent.control.vm import create_overlay

    _fake_qemu_img(monkeypatch)
    base = tmp_path / "blank.qcow2"
    base.write_bytes(b"QFI\xfb" + b"\x00" * 4092)
    target = tmp_path / "sessions" / "s1" / "workspace.qcow2"

    create_overlay(base, target)
    assert target.read_bytes() == b"overlay"
    stamp = target.parent / ".base-fingerprint"
    assert stamp.is_file()

    target.write_bytes(b"user data")
    create_overlay(base, target)
    assert target.read_bytes() == b"user data", "an unchanged base must not wipe the session"


def test_overlay_is_discarded_when_the_base_changes(tmp_path: Path, monkeypatch):
    from agent.control.vm import create_overlay

    _fake_qemu_img(monkeypatch)
    base = tmp_path / "blank.qcow2"
    base.write_bytes(b"QFI\xfb" + b"\x00" * 4092)
    target = tmp_path / "sessions" / "s1" / "workspace.qcow2"

    create_overlay(base, target)
    first = (target.parent / ".base-fingerprint").read_text(encoding="utf-8")
    target.write_bytes(b"user data")

    base.write_bytes(b"QFI\xfb" + b"\x01" * 4092)  # rebuilt image
    create_overlay(base, target)

    assert target.read_bytes() == b"overlay", "the stale overlay must be recreated"
    assert (target.parent / ".base-fingerprint").read_text(encoding="utf-8") != first
