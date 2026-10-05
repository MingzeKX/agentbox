"""Inline images: a reply (or a tool result) that names a sandbox picture shows it.

The scan is pure text, and the pull reuses ``/get``'s machinery, so both run here with the
control plane stubbed: no VM and no network.  Pillow is *not* a host dependency, which is
why the half-block painter must answer ``False`` -- and never raise -- when it cannot decode
the file: the operator still gets the ``图片: var\\pulled\\...`` path line.
"""

from __future__ import annotations

import base64
import hashlib
import sys
import types

import pytest
from rich.console import Console

from agent.cli.console import (
    ConsoleState,
    SlashConsole,
    render_image_blocks,
    scan_image_paths,
)


class _FakeImage:
    """The little bit of the Pillow API the half-block painter uses."""

    def __init__(self, size: tuple[int, int] = (4, 4)) -> None:
        self.size = size

    def __enter__(self):
        return self

    def __exit__(self, *exc):  # noqa: ANN002, ARG002
        return False

    def convert(self, mode: str):  # noqa: ARG002
        return self

    def resize(self, size: tuple[int, int]):
        self.size = size
        return self

    def load(self):
        return _FakePixels(self.size)


class _FakePixels:
    def __init__(self, size: tuple[int, int]) -> None:
        self.size = size

    def __getitem__(self, xy: tuple[int, int]) -> tuple[int, int, int]:
        x, y = xy
        return (x % 256, y % 256, (x + y) % 256)


@pytest.fixture
def shell():
    console = Console(record=True, width=120, no_color=True, force_terminal=False)
    state = ConsoleState(log_style="ide", log_lines=24)
    return SlashConsole(console, state, console), console, state


def output(console: Console) -> str:
    return console.export_text()


def test_the_scanner_finds_sandbox_images_once_and_ignores_everything_else():
    text = (
        "做好了：/workspace/a.PNG\n"
        "![shot](/workspace/b.jpg) 还有 `/workspace/c.bmp`。\n"
        "再看 /workspace/report.txt、/workspace/notes.py、/etc/passwd、"
        "https://example.com/d.png，以及 /workspace/a.PNG 这同一张。\n"
    )

    assert scan_image_paths(text) == ["/workspace/a.PNG", "/workspace/b.jpg", "/workspace/c.bmp"]
    assert scan_image_paths("") == []
    assert scan_image_paths(None) == []


def test_inline_images_are_pulled_like_get_capped_deduped_and_degrade_without_pillow(shell, tmp_path, monkeypatch):
    sh, console, state = shell
    state.session_id = "s-test"
    payload = b"\x89PNG\r\n\x1a\n" + b"7" * 100
    calls: list[tuple[str, dict]] = []

    def fake_rpc(method, params=None, timeout=120.0):  # noqa: ANN001
        calls.append((method, dict(params or {})))
        if method == "sandbox.invoke":
            path = str(params["params"]["path"])
            return {
                "ok": True,
                "result": {
                    "path": path,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "truncated": False,
                    "data_b64": base64.b64encode(payload).decode("ascii"),
                },
            }
        return {"ok": True, "path": str(tmp_path / "pulled" / str(params["dest"])), "bytes": len(payload)}

    monkeypatch.setattr("agent.cli.main._control_rpc", fake_rpc)

    shown = sh.show_inline_images("图：/workspace/a.png、/workspace/b.png、/workspace/c.png")

    assert shown == 2, "at most two images per turn"
    assert [c[1]["params"]["path"] for c in calls if c[0] == "sandbox.invoke"] == [
        "/workspace/a.png",
        "/workspace/b.png",
    ]
    # exactly what /get does with the bytes: one host.pull.write per file, no /admin/config
    assert [c[1]["dest"] for c in calls if c[0] == "host.pull.write"] == ["a.png", "b.png"]
    assert sh.show_inline_images("/workspace/a.png") == 0, "one picture is pulled once per session"

    text = output(console)
    assert "图片: var\\pulled\\a.png (108 字节)" in text, "the operator must get the file path back"
    assert "c.png" not in text

    # Pillow is optional on the host: no Pillow (or no image) is a quiet False, not a crash
    assert render_image_blocks(console, tmp_path / "pulled" / "missing.png") is False
    garbage = tmp_path / "garbage.png"
    garbage.write_bytes(b"not an image at all")
    assert render_image_blocks(console, garbage) is False

    # ...and when an image library *can* decode it, the picture itself is painted
    module = types.ModuleType("PIL")
    module.Image = types.SimpleNamespace(open=lambda path: _FakeImage())  # noqa: ARG005
    monkeypatch.setitem(sys.modules, "PIL", module)
    assert render_image_blocks(console, tmp_path / "anything.png") is True
    assert "▀" in output(console)
