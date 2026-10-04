"""A poisoned session must not stay poisoned.

Two independent defects made a session permanently unusable -- every later turn answered
HTTP 400 within ~200 ms, before the model ever ran, and only ``/new`` recovered:

1. **the history window could start on a ``tool`` message** (root cause).  The window keeps
   the newest ``max_history_messages`` rows, and the agent loop adds one ``assistant``
   message with ``tool_calls`` plus one ``tool`` row per call, so the boundary regularly
   fell inside a pair.  A ``tool`` message whose ``tool_call_id`` no message in the window
   declares is invalid for the API -- and it stayed in the window forever.
2. **a tool result was stored unbounded** (parallel defect).  A multi-megabyte
   ``pip install`` transcript, ``net.fetch`` body or ``toolsmith.create`` source echo went
   into ``messages`` verbatim and was then replayed on every later turn.

These tests pin the guards: the window is trimmed to whole pairs, what is *stored* is
capped, and what is *sent* is re-sanitised at the single chokepoint every request uses.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.ai.agent_loop import AgentLoop
from agent.ai.history import (
    HISTORY_MESSAGE_MAX_CHARS,
    HISTORY_TOOL_MAX_CHARS,
    drop_leading_orphans,
    for_history,
    sanitize_history,
    truncate_for_history,
)
from agent.ai.llm import LLMClient, LLMError, LLMResult, LLMToolCall
from agent.registry import repository as repo


class ScriptedLLM:
    model = "scripted-model"

    def __init__(self, results: list[LLMResult | Exception]) -> None:
        self.results = list(results)
        self.requests: list[list[dict[str, Any]]] = []

    async def chat(self, messages, *, tools=None, temperature=None, model=None, on_token=None):
        self.requests.append([dict(message) for message in messages])
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if on_token and result.content:
            on_token(result.content)
        return result


class StubMeta:
    specs = [{"type": "function", "function": {"name": "search_tools", "parameters": {}}}]

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name: str, args: dict, version=None, timeout_s=None):
        from agent.ai.metacalls import DispatchOutcome

        self.calls.append((name, args))
        return DispatchOutcome(True, tool=name, result={"echo": args}, duration_ms=7)


def build_loop(llm, meta, events, sessionmaker, max_steps: int = 4) -> AgentLoop:
    return AgentLoop(
        session_id="s-test",
        llm=llm,  # type: ignore[arg-type]
        meta=meta,  # type: ignore[arg-type]
        sessionmaker=sessionmaker,  # type: ignore[arg-type]
        on_event=lambda kind, payload: events.append((kind, payload)),
        max_steps=max_steps,
    )


@pytest.fixture
def stored(monkeypatch):
    """Capture what the loop writes to the session, and feed it back as history."""
    rows: list[tuple[str, dict[str, Any]]] = []

    async def load_messages(session, session_id, limit=40):
        class Row:
            def __init__(self, content):
                self.content = content

        return [Row(content) for _, content in rows]

    async def ensure_session(session, session_id, title=""):
        return None

    async def append_message(session, session_id, role, content, tokens=0):
        rows.append((role, content))

    monkeypatch.setattr(repo, "load_messages", load_messages)
    monkeypatch.setattr(repo, "ensure_session", ensure_session)
    monkeypatch.setattr(repo, "append_message", append_message)
    return rows


# --------------------------------------------------------------------------- ingest


def test_truncate_for_history_keeps_head_tail_and_says_what_was_dropped():
    text = "HEAD" + "x" * 50_000 + "TAIL"

    capped = truncate_for_history(text, 1000)

    assert len(capped) == 1000, "the cap is a hard cap"
    assert capped.startswith("HEAD"), "the head explains what the command was"
    assert capped.endswith("TAIL"), "the tail carries the error/final output"
    assert "已截断" in capped and "/more" in capped
    assert truncate_for_history("short", 1000) == "short", "nothing is added when it fits"
    assert truncate_for_history("x" * 10, 0) == ""


def test_for_history_stores_text_only_and_never_the_base64_image():
    parts = [
        {"type": "text", "text": "看看这张图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5_000_000}},
    ]

    stored = for_history({"role": "user", "content": parts})

    assert stored["role"] == "user"
    assert "看看这张图" in stored["content"]
    assert "图片 ×1" in stored["content"], "the picture is recorded as a short placeholder"
    assert "base64" not in stored["content"], "the payload must never enter the history"
    assert len(stored["content"]) < 200

    # a plain string is untouched
    assert for_history({"role": "user", "content": "hi"})["content"] == "hi"


def test_an_oversized_tool_result_never_enters_the_history():
    """The regression for the parallel defect: 2 MB of tool output must not be stored."""
    tool_message = {
        "role": "tool",
        "tool_call_id": "c1",
        "name": "net.fetch",
        "content": '{"text": "' + "x" * 2_000_000 + '"}',
    }

    stored = for_history(tool_message)

    assert len(stored["content"]) <= HISTORY_MESSAGE_MAX_CHARS
    assert "已截断" in stored["content"]
    assert "完整内容见 /more" in stored["content"]


# --------------------------------------------------------------------------- egress


def _assistant_with_call(call_id: str = "c1") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": "exec.run", "arguments": "{}"}}
        ],
    }


def test_drop_leading_orphans_never_returns_a_tool_message_first():
    """Root cause: the window boundary cut a tool_call/tool result pair in half."""
    window = [
        {"role": "tool", "tool_call_id": "gone", "content": "orphan"},
        {"role": "tool", "tool_call_id": "gone2", "content": "orphan"},
        _assistant_with_call("c3"),
        {"role": "tool", "tool_call_id": "c3", "content": "answered"},
        {"role": "user", "content": "next question"},
    ]

    trimmed = drop_leading_orphans(window)

    assert [message["role"] for message in trimmed] == ["assistant", "tool", "user"]
    assert trimmed[0]["tool_calls"][0]["id"] == "c3"
    assert trimmed[1]["tool_call_id"] == "c3", "the pair inside the window is kept"


def test_drop_leading_orphans_leaves_a_clean_window_alone():
    window = [{"role": "user", "content": "hi"}, _assistant_with_call(), {"role": "tool", "tool_call_id": "c1"}]
    assert drop_leading_orphans(window) == window
    assert drop_leading_orphans([]) == []


def test_sanitize_drops_orphaned_tool_messages():
    messages = [
        {"role": "system", "content": "contract"},
        {"role": "tool", "tool_call_id": "outside-the-window", "content": "orphan"},
        {"role": "user", "content": "hello"},
    ]

    safe = sanitize_history(messages)

    assert [message["role"] for message in safe] == ["system", "user"]


def test_sanitize_drops_tool_calls_that_nothing_answers():
    """An interrupted step must not leave a dangling ``tool_calls`` declaration."""
    messages = [
        {"role": "system", "content": "contract"},
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "working",
            "tool_calls": [
                {"id": "answered", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                {"id": "lost", "type": "function", "function": {"name": "b", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "answered", "name": "a", "content": "ok"},
        {"role": "user", "content": "and now?"},
    ]

    safe = sanitize_history(messages)

    assistant = next(message for message in safe if message["role"] == "assistant")
    assert [call["id"] for call in assistant["tool_calls"]] == ["answered"]
    assert [message["role"] for message in safe] == ["system", "user", "assistant", "tool", "user"]


def test_sanitize_drops_an_assistant_that_has_neither_text_nor_calls():
    safe = sanitize_history([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "   "}])
    assert [message["role"] for message in safe] == ["user"]


def test_sanitize_drops_malformed_tool_calls_and_blank_messages():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "x", "tool_calls": [{"id": "c", "function": {}}]},
        {"role": "user", "content": "   "},
        {"role": "user", "content": "real"},
    ]

    safe = sanitize_history(messages)

    # the unusable declaration goes, but the message still carries text, so it stays
    assert [message["role"] for message in safe] == ["user", "assistant", "user"]
    assistant = next(message for message in safe if message["role"] == "assistant")
    assert "tool_calls" not in assistant, "a call with no function name is not sendable"
    assert assistant["content"] == "x"
    assert [message["content"] for message in safe if message["role"] == "user"] == ["hi", "real"]


def test_sanitize_strips_history_images_but_keeps_the_current_turn():
    """A replayed screenshot is megabytes the model no longer needs; the turn being sent
    keeps its parts so vision still works."""
    old_image = [{"type": "text", "text": "old"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    messages = [
        {"role": "system", "content": "contract"},
        {"role": "user", "content": old_image},
        {"role": "assistant", "content": "I saw it"},
        {"role": "user", "content": [{"type": "text", "text": "new"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}}]},
    ]

    safe = sanitize_history(messages)

    assert isinstance(safe[1]["content"], str) and "base64" not in safe[1]["content"]
    assert isinstance(safe[-1]["content"], list), "the turn being sent keeps its image part"
    assert safe[-1]["content"][1]["image_url"]["url"].endswith("BBBB")


def test_sanitize_bounds_the_total_budget_and_says_so():
    messages = [{"role": "system", "content": "contract"}]
    messages += [{"role": "user", "content": f"turn {index} " + "x" * 4_000} for index in range(50)]

    safe = sanitize_history(messages, max_chars=20_000)

    total = sum(len(str(message.get("content"))) for message in safe)
    assert total <= 20_000
    assert safe[0]["role"] == "system"
    assert "超出上下文预算" in safe[0]["content"], "the model is told history was dropped"
    assert safe[-1]["content"].startswith("turn 49"), "the newest turn always survives"


def test_payload_uses_the_sanitized_messages():
    """The chokepoint: no caller may send a request that was not sanitised."""
    raw = [
        {"role": "system", "content": "contract"},
        {"role": "tool", "tool_call_id": "ghost", "content": "orphan"},
        {"role": "user", "content": "x" * 5_000_000},
    ]

    payload = LLMClient(api_key="k")._payload(raw, None, 0.0, None, stream=True)

    assert [message["role"] for message in payload["messages"]] == ["system", "user"]
    assert len(str(payload["messages"][-1]["content"])) <= HISTORY_MESSAGE_MAX_CHARS
    assert len(str(payload)) < 100_000, "the whole body stays bounded"


# --------------------------------------------------------------------------- the loop


def test_an_oversized_tool_result_is_capped_before_it_is_stored():
    """Whatever the tool returned, the row that will be replayed every turn is capped."""
    tool_message = {"role": "tool", "tool_call_id": "c1", "name": "exec.run", "content": "x" * 5_000_000}
    stored = for_history(tool_message)
    assert len(stored["content"]) == HISTORY_MESSAGE_MAX_CHARS


@pytest.mark.asyncio
async def test_the_loop_caps_the_tool_result_it_feeds_back(fake_sessionmaker, stored):
    events: list[tuple[str, dict]] = []
    call = LLMToolCall(id="c1", name="search_tools", arguments={}, raw_arguments="{}")
    llm = ScriptedLLM(
        [
            LLMResult(tool_calls=[call], finish_reason="tool_calls"),
            LLMResult(content="done", finish_reason="stop"),
        ]
    )

    class HugeMeta(StubMeta):
        async def call(self, name: str, args: dict, version=None, timeout_s=None):
            from agent.ai.metacalls import DispatchOutcome

            self.calls.append((name, args))
            return DispatchOutcome(True, tool=name, result={"stdout": "y" * 2_000_000}, duration_ms=1)

    loop = build_loop(llm, HugeMeta(), events, fake_sessionmaker)
    await loop.run("run something noisy")

    tool_rows = [content for role, content in stored if role == "tool"]
    assert tool_rows, "the tool result was stored"
    assert len(tool_rows[0]["content"]) <= HISTORY_TOOL_MAX_CHARS
    assert "已截断" in tool_rows[0]["content"]

    # and what was actually sent to the model is bounded too
    sent = llm.requests[1][-1]
    assert len(sent["content"]) <= HISTORY_TOOL_MAX_CHARS


@pytest.mark.asyncio
async def test_a_session_holding_an_orphaned_tool_message_still_answers(fake_sessionmaker, stored):
    """The poisoned-session regression: a bad row must not fail the *next* turn."""
    stored.append(("tool", {"role": "tool", "tool_call_id": "outside-the-window", "content": "orphan"}))
    stored.append(("assistant", {"role": "assistant", "content": "earlier answer"}))
    events: list[tuple[str, dict]] = []
    llm = ScriptedLLM([LLMResult(content="recovered fine", finish_reason="stop")])
    loop = build_loop(llm, StubMeta(), events, fake_sessionmaker)

    result = await loop.run("are you still there?")

    assert result.stop_reason == "stop"
    assert result.content == "recovered fine"
    sent = llm.requests[0]
    assert not any(message["role"] == "tool" for message in sent), "the orphan never left the process"
    assert [message["role"] for message in sent] == ["system", "assistant", "user"]


@pytest.mark.asyncio
async def test_a_4xx_aborts_the_turn_with_zero_tool_calls_and_shows_the_gateway_body(fake_sessionmaker, stored):
    body = '{"error":{"message":"Invalid parameter: messages with role tool must be a response to a preceding message with tool_calls","type":"invalid_request_error"}}'
    events: list[tuple[str, dict]] = []
    meta = StubMeta()
    llm = ScriptedLLM([LLMError("LLM endpoint returned HTTP 400", 400, body)])
    loop = build_loop(llm, meta, events, fake_sessionmaker)

    result = await loop.run("send the email")

    assert result.stop_reason == "llm_error"
    assert result.steps == [], "a 4xx must abort the turn before any tool runs"
    assert meta.calls == [], "and no tool was called"
    assert not [row for row in stored if row[0] == "tool"], "nothing was written as a tool result"

    # the operator sees the gateway's own words, not just "HTTP 400"
    shown = [payload["message"] for kind, payload in events if kind == "error"]
    assert shown and "must be a response to a preceding message with tool_calls" in shown[0]
    assert "HTTP 400" in shown[0]
    assert "must be a response to a preceding message" in result.content


def test_llm_error_detail_truncates_a_huge_gateway_body():
    error = LLMError("LLM endpoint returned HTTP 400", 400, "z" * 5_000)
    detail = error.detail(limit=200)
    assert len(detail) < 300
    assert detail.endswith("…")
    assert LLMError("boom").detail() == "boom", "no body -> unchanged message"


# --------------------------------------------------- the shape found in the live DB
# Session s-1fcbb55f39c74adc (35 messages, so the whole history is sent on every turn)
# held an assistant message declaring call_00_jdGnCpyoJAkRSuGAUJqT2756 with no tool
# message answering it: the tool call had died ("control plane unreachable"), so the loop
# persisted the declaration and never the result.  Every later turn -- including the
# operator's retries -- was rejected with HTTP 400 in ~200 ms until /new.


def _live_poison() -> list[dict[str, Any]]:
    """The stored history of s-1fcbb55f39c74adc, in shape."""
    dangling = _assistant_with_call("call_00_jdGnCpyoJAkRSUGAUJqT2756")
    # the real row carried the model's explanation of the failed registration
    dangling["content"] = "注册这一步失败了，但不是我的代码问题——是控制平面不可达"
    return [
        {"role": "system", "content": "contract"},
        {"role": "user", "content": "你好啊"},
        dangling,
        {"role": "user", "content": "请问你是谁"},
    ]


def test_a_dangling_tool_call_from_a_crashed_tool_is_pruned():
    safe = sanitize_history(_live_poison())

    assistant = next(message for message in safe if message["role"] == "assistant")
    assert "tool_calls" not in assistant, "an unanswered declaration is what the gateway rejects"
    assert "控制平面不可达" in assistant["content"], "the model's own words are kept"
    assert [message["role"] for message in safe] == ["system", "user", "assistant", "user"]
    assert [message["content"] for message in safe if message["role"] == "user"] == ["你好啊", "请问你是谁"]


def test_a_dangling_tool_call_with_no_text_is_dropped_whole():
    """Nothing is left for the model to read, so the message itself goes."""
    safe = sanitize_history(
        [{"role": "user", "content": "hi"}, _assistant_with_call("ghost"), {"role": "user", "content": "still there?"}]
    )
    assert [message["role"] for message in safe] == ["user", "user"]


@pytest.mark.asyncio
async def test_a_crashed_tool_call_is_still_answered_so_history_stays_valid(fake_sessionmaker, stored):
    """The write side: a tool that raises must still produce a `tool` row."""
    events: list[tuple[str, dict]] = []
    call = LLMToolCall(id="call_dead", name="call_tool", arguments={}, raw_arguments="{}")
    llm = ScriptedLLM(
        [
            LLMResult(tool_calls=[call], finish_reason="tool_calls"),
            LLMResult(content="recovered", finish_reason="stop"),
        ]
    )

    class CrashingMeta(StubMeta):
        async def call(self, name: str, args: dict, version=None, timeout_s=None):
            raise RuntimeError("control plane unreachable")

    loop = build_loop(llm, CrashingMeta(), events, fake_sessionmaker)
    result = await loop.run("create the tool")

    assert result.stop_reason == "stop", "one crashing tool must not end the turn"
    assert result.steps[0].ok is False
    tool_rows = [content for role, content in stored if role == "tool"]
    assert len(tool_rows) == 1, "the declaration was answered, so the history stays valid"
    assert tool_rows[0]["tool_call_id"] == "call_dead"
    assert "handler_raised" in tool_rows[0]["content"] or "crashed" in tool_rows[0]["content"]

    # what actually goes out is valid: the calls the assistant declares are all answered
    second = llm.requests[1]
    declared = {
        call["id"]
        for message in second
        if message["role"] == "assistant"
        for call in message.get("tool_calls") or []
    }
    answered = {message["tool_call_id"] for message in second if message["role"] == "tool"}
    assert declared == answered == {"call_dead"}
