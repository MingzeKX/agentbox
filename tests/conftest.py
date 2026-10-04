"""Shared pytest fixtures.

The unit suite must run without PostgreSQL, without QEMU and without torch, so
everything heavy is replaced by deterministic fakes:

* :class:`FakeEmbedder`  - hashing based vectors of the right dimension
* :class:`FakeGateway`   - records sandbox calls, replays canned responses
* :class:`FakeSessionmaker` - a no-op async session so registry lookups can be
  monkeypatched without a database
"""

from __future__ import annotations

import hashlib
import math
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from agent.models.tool import SandboxInvokeResult


@pytest.fixture(scope="session", autouse=True)
def _tempdir_inside_var() -> None:
    """Keep every scratch directory out of the repository root.

    On this machine TEMP resolves to the workspace, so bare ``tempfile.mkdtemp()`` calls
    (and pytest's own base temp) littered the checkout with empty ``tmp*/`` and
    ``pytest-of-*/`` directories.  ``var/`` is gitignored and already means "runtime
    state", so that is where scratch belongs.
    """
    scratch = Path(__file__).resolve().parents[1] / "var" / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    previous = tempfile.tempdir
    tempfile.tempdir = str(scratch)
    try:
        yield
    finally:
        tempfile.tempdir = previous


class FakeEmbedder:
    """Deterministic bag-of-words embedding; no model download."""

    name = "fake-embedder"
    dim = 1024
    available = True

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:2], "big") % self.dim
            vector[index] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    async def embed_one(self, text: str) -> list[float]:
        return self._vector(text)


class FakeGateway:
    """Stands in for SandboxGateway, recording every call."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, Any]] = []

    def _respond(self, key: str, default: Any) -> SandboxInvokeResult:
        payload = self.responses.get(key, default)
        if isinstance(payload, SandboxInvokeResult):
            return payload
        return SandboxInvokeResult(ok=True, result=payload, duration_ms=1, vm_id="vm-fake")

    async def invoke_native(self, session_id: str, method: str, params: dict, timeout_s: float | None = None):
        self.calls.append(("native", {"method": method, "params": params, "session_id": session_id}))
        return self._respond(f"native:{method}", {})

    async def invoke_tool(self, session_id: str, tool, arguments: dict, timeout_s: float | None = None):
        self.calls.append(("tool", {"tool": tool.name, "arguments": arguments, "session_id": session_id}))
        return self._respond(f"tool:{tool.name}", {"ok": True, "result": {"echo": arguments}})

    async def acquire(self, session_id: str):
        self.calls.append(("acquire", {"session_id": session_id}))
        return None

    async def release(self, session_id: str) -> bool:
        self.calls.append(("release", {"session_id": session_id}))
        return True

    async def status(self):
        self.calls.append(("status", {}))
        raise AssertionError("status is not expected in this test")


class _DummySession:
    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def __aenter__(self) -> _DummySession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeSessionmaker:
    """Callable returning an async context manager, mirroring async_sessionmaker."""

    def __call__(self) -> _DummySession:
        return _DummySession()

    @asynccontextmanager
    async def scope(self) -> AsyncIterator[_DummySession]:
        yield _DummySession()


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def fake_gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def fake_sessionmaker() -> FakeSessionmaker:
    return FakeSessionmaker()


@pytest.fixture
def settings_factory(tmp_path):
    """Build Settings objects with a throwaway sandbox directory.

    ``base_kernel``/``base_rootfs``/... are derived properties, so tests that need
    image files create them in the temporary sandbox directory.
    """
    from agent.config import Settings

    def factory(**overrides):
        sandbox_dir = overrides.pop("sandbox_dir", tmp_path / "sandbox")
        return Settings(
            var_dir=overrides.pop("var_dir", tmp_path),
            sandbox_dir=sandbox_dir,
            sandbox_pool_size=overrides.pop("sandbox_pool_size", 0),
            rpc_token=overrides.pop("rpc_token", ""),
            **overrides,
        )

    return factory
