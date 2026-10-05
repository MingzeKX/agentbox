"""``/chat`` turns of one session are serialized, turns of different sessions are not.

Live root cause of a recurring, session-killing HTTP 400: the streaming endpoint started
one task per request with no per-session mutual exclusion, so two turns of the same
session interleaved their DB writes -- an ``assistant`` row declaring a ``tool_call``,
then a ``user`` row, and the answering ``tool`` row only 12 rows later.  The gateway
rejects that history forever; commit ``9363383`` repairs it downstream (a ``tool_call`` is
answered by the messages that directly follow it), and the per-session lock exercised
here is the prevention.

No PostgreSQL, no network, no LLM: the endpoint is called directly, its session store is
a fake that records the order of the writes, and the agent loop is replaced by a stub.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from agent.ai import app as app_module
from agent.ai.app import ChatRequest

SESSION = "s-lock-test"


class FakeStore:
    """The session's database, reduced to the order of its writes."""

    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, label: str) -> None:
        self.writes.append(label)


class _FakeSession:
    async def commit(self) -> None:
        # a real commit suspends; keeping that makes an unserialized second turn able to
        # slip its writes in between the first turn's
        await asyncio.sleep(0)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSessionmaker:
    def __call__(self) -> _FakeSession:
        return _FakeSession()


def _fake_loop(store: FakeStore, on_run=None):
    """The ``AgentLoop`` stand-in: writes the reply and yields control mid-turn."""

    class _Loop:
        def __init__(self, **kwargs: Any) -> None:
            self.on_event = kwargs.get("on_event")

        async def run(self, user_message, system_extra=""):
            if on_run is not None:
                await on_run(user_message)
            store.write(f"reply:{user_message}")
            return SimpleNamespace(content=f"reply to {user_message}", stop_reason="stop", steps=[], usage={})

    return _Loop


def _chat_endpoint(monkeypatch, store: FakeStore, on_run=None):
    """``POST /chat`` wired to the fake store, the fake loop and no network."""

    async def fake_ensure_session(session, session_id, title=""):
        store.write(f"ensure:{title}")

    async def fake_runtime_facts(gateway_client):
        return ""

    app = app_module.create_app()
    app.state.gateway = SimpleNamespace()
    app.state.llm = SimpleNamespace()
    app.state.embedder = SimpleNamespace(name="fake", dim=1, available=False)
    monkeypatch.setattr(app_module.registry_db, "get_sessionmaker", lambda: _FakeSessionmaker())
    monkeypatch.setattr(app_module.repo, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(app_module, "_runtime_facts", fake_runtime_facts)
    monkeypatch.setattr(app_module, "AgentLoop", _fake_loop(store, on_run=on_run))
    route = next(route for route in app.routes if getattr(route, "path", None) == "/chat")
    return app, route.endpoint


@pytest.mark.asyncio
async def test_two_turns_of_the_same_session_are_serialized(monkeypatch):
    store = FakeStore()
    app, endpoint = _chat_endpoint(monkeypatch, store)

    first = asyncio.create_task(endpoint(ChatRequest(message="first", session_id=SESSION, stream=False)))
    second = asyncio.create_task(endpoint(ChatRequest(message="second", session_id=SESSION, stream=False)))
    responses = await asyncio.gather(first, second)

    assert [response["content"] for response in responses] == ["reply to first", "reply to second"]
    # the two turns never interleave: the second turn's first write (its ensure_session)
    # happens only after the first turn's last write (its reply), which is exactly the
    # property whose absence produced the unreplayable history
    assert store.writes in (
        ["ensure:first", "reply:first", "ensure:second", "reply:second"],
        ["ensure:second", "reply:second", "ensure:first", "reply:first"],
    ), store.writes
    # and the lock is forgotten again once the session has no holder and no waiter
    assert SESSION not in app_module._session_locks
    assert SESSION not in app_module._session_turns


@pytest.mark.asyncio
async def test_different_sessions_still_run_concurrently(monkeypatch):
    store = FakeStore()
    inside: list[str] = []
    both_inside = asyncio.Event()

    async def wait_for_the_other_turn(user_message):
        inside.append(user_message)
        if len(inside) == 2:
            both_inside.set()
        # a global lock would keep one of the two turns out here and time the test out
        await asyncio.wait_for(both_inside.wait(), timeout=5.0)

    _, endpoint = _chat_endpoint(monkeypatch, store, on_run=wait_for_the_other_turn)

    responses = await asyncio.gather(
        endpoint(ChatRequest(message="a", session_id="s-a", stream=False)),
        endpoint(ChatRequest(message="b", session_id="s-b", stream=False)),
    )

    assert sorted(inside) == ["a", "b"], "both turns were inside the agent loop at once"
    assert len(responses) == 2
    assert app_module._session_locks == {}
    assert app_module._session_turns == {}
