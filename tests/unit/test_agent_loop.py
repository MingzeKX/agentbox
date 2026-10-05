"""The agent loop: resident tools only, results fed back, budgets enforced."""

from __future__ import annotations

from typing import Any

import pytest

from agent.ai import personas
from agent.ai.agent_loop import AgentLoop, load_system_prompt
from agent.ai.llm import LLMError, LLMResult, LLMToolCall
from agent.registry import repository as repo


class ScriptedLLM:
    model = "scripted-model"

    def __init__(self, results: list[LLMResult | Exception]) -> None:
        self.results = list(results)
        self.requests: list[list[dict[str, Any]]] = []

    async def chat(self, messages, *, tools=None, temperature=None, model=None, on_token=None):
        self.requests.append([dict(message) for message in messages])
        assert tools, "the loop must always expose the resident meta tools"
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if on_token and result.content:
            on_token(result.content)
        return result


class StubMeta:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    specs = [{"type": "function", "function": {"name": "search_tools", "parameters": {}}}]

    async def call(self, name: str, args: dict, version=None, timeout_s=None):
        from agent.ai.metacalls import DispatchOutcome

        self.calls.append((name, args))
        if name == "boom":
            return DispatchOutcome(False, tool="boom", error="sandbox exploded", error_code="sandbox_error")
        return DispatchOutcome(True, tool=name, result={"echo": args}, duration_ms=7)


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    async def load_messages(session, session_id, limit=40):
        return []

    async def ensure_session(session, session_id, title=""):
        return None

    async def append_message(session, session_id, role, content, tokens=0):
        return None

    monkeypatch.setattr(repo, "load_messages", load_messages)
    monkeypatch.setattr(repo, "ensure_session", ensure_session)
    monkeypatch.setattr(repo, "append_message", append_message)


def build_loop(llm, meta, events, fake_sessionmaker, max_steps: int = 4) -> AgentLoop:
    return AgentLoop(
        session_id="s-test",
        llm=llm,  # type: ignore[arg-type]
        meta=meta,  # type: ignore[arg-type]
        sessionmaker=fake_sessionmaker,  # type: ignore[arg-type]
        on_event=lambda kind, payload: events.append((kind, payload)),
        max_steps=max_steps,
    )


@pytest.mark.asyncio
async def test_direct_answer_without_tool_calls(fake_sessionmaker):
    events: list[tuple[str, dict]] = []
    llm = ScriptedLLM([LLMResult(content="all done", finish_reason="stop")])
    loop = build_loop(llm, StubMeta(), events, fake_sessionmaker)

    result = await loop.run("hello")

    assert result.content == "all done"
    assert result.stop_reason == "stop"
    assert result.steps == []
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "start"
    assert "token" in kinds
    assert kinds[-1] == "done"


@pytest.mark.asyncio
async def test_tool_call_round_trip_feeds_the_result_back(fake_sessionmaker):
    events: list[tuple[str, dict]] = []
    call = LLMToolCall(id="call_1", name="search_tools", arguments={"query": "read a file"}, raw_arguments="{}")
    llm = ScriptedLLM(
        [
            LLMResult(content="", tool_calls=[call], finish_reason="tool_calls"),
            LLMResult(content="I found fs.read", finish_reason="stop"),
        ]
    )
    meta = StubMeta()
    loop = build_loop(llm, meta, events, fake_sessionmaker)

    result = await loop.run("read a file please")

    assert meta.calls == [("search_tools", {"query": "read a file"})]
    assert result.content == "I found fs.read"
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.tool == "search_tools"
    assert step.ok is True
    assert step.duration_ms == 7

    # the second LLM request must contain the assistant tool_call and the tool result
    second = llm.requests[1]
    roles = [message["role"] for message in second]
    assert roles[-2:] == ["assistant", "tool"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "search_tools"
    assert '"echo"' in second[-1]["content"]


@pytest.mark.asyncio
async def test_failed_tool_call_is_reported_to_the_model(fake_sessionmaker):
    events: list[tuple[str, dict]] = []
    call = LLMToolCall(id="c1", name="boom", arguments={}, raw_arguments="{}")
    llm = ScriptedLLM(
        [
            LLMResult(tool_calls=[call], finish_reason="tool_calls"),
            LLMResult(content="recovered", finish_reason="stop"),
        ]
    )
    loop = build_loop(llm, StubMeta(), events, fake_sessionmaker)

    result = await loop.run("trigger a failure")

    assert result.steps[0].ok is False
    tool_message = llm.requests[1][-1]
    assert '"ok":false' in tool_message["content"].replace(" ", "")
    assert "sandbox exploded" in tool_message["content"]
    assert result.content == "recovered"


@pytest.mark.asyncio
async def test_step_budget_is_enforced(fake_sessionmaker):
    events: list[tuple[str, dict]] = []
    call = LLMToolCall(id="c", name="search_tools", arguments={}, raw_arguments="{}")
    llm = ScriptedLLM([LLMResult(tool_calls=[call], finish_reason="tool_calls") for _ in range(10)])
    loop = build_loop(llm, StubMeta(), events, fake_sessionmaker, max_steps=3)

    result = await loop.run("loop forever")

    assert result.stop_reason == "max_steps"
    assert "stopped after 3 steps" in result.content
    assert len(result.steps) == 3


@pytest.mark.asyncio
async def test_llm_failure_is_reported_not_raised(fake_sessionmaker):
    events: list[tuple[str, dict]] = []
    llm = ScriptedLLM([LLMError("endpoint down", 503, "busy")])
    loop = build_loop(llm, StubMeta(), events, fake_sessionmaker)

    result = await loop.run("hi")

    assert result.stop_reason == "llm_error"
    assert "endpoint down" in result.content
    assert any(kind == "error" for kind, _ in events)


@pytest.mark.asyncio
async def test_system_prompt_carries_the_resident_tool_contract(fake_sessionmaker):
    events: list[tuple[str, dict]] = []
    llm = ScriptedLLM([LLMResult(content="ok")])
    loop = build_loop(llm, StubMeta(), events, fake_sessionmaker)

    await loop.run("anything")

    system = llm.requests[0][0]
    assert system["role"] == "system"
    for tool in ("search_tools", "get_tool_schema", "call_tool"):
        assert tool in system["content"]
    assert "/workspace" in system["content"]
    assert "s-test" in system["content"]


def test_system_prompt_pins_authorization_and_honesty_after_the_persona():
    """The real assembled prompt, not a stub: the operator's own material authorizes
    the task, a result may never be invented, and both rules sit after the persona."""
    prompt = load_system_prompt("s-sendmail")

    assert "# 授权、诚实与拒绝纪律" in prompt
    assert "主人自己交来的凭据 + 明确指令 = 授权本身" in prompt
    assert "涉及敏感信息、凭据、密码、验证码、私钥" in prompt
    assert "严禁编造工具返回值" in prompt
    assert "逐字引用原始错误文本" in prompt
    # the fix must name a real, existing console command, not an invented one
    assert "/perm trusted" in prompt
    assert "/config tool_extra_modules smtplib" in prompt

    persona_at = prompt.index("# Persona: ")
    assert prompt.index("主人自己交来的凭据 + 明确指令 = 授权本身") > persona_at
    assert prompt.index("严禁编造工具返回值") > persona_at

    # the delivery routes are real, and they must be named after the persona too:
    # fs.pull to var\pulled\, the inline image path, /get, and the plain fact that
    # the sandbox is online. Without these the agent denies having a channel at all.
    assert "fs.pull" in prompt
    assert "var\\pulled\\" in prompt
    assert "/workspace/chart.png" in prompt
    assert "/get <沙箱路径>" in prompt
    assert "默认是联网的" in prompt
    assert "/net allow <域名>" in prompt
    for rule in ("`fs.pull` 推到主机的", "默认是联网的"):
        assert prompt.index(rule) > persona_at


@pytest.mark.parametrize("name", personas.names())
def test_no_persona_can_override_or_precede_the_operating_rules(name):
    """Every shipped persona loads, and none of them can drop or outrank the rules."""
    persona = personas.load(name)
    if not persona.text:
        pytest.skip(f"{name} has an empty body")

    prompt = load_system_prompt("s-persona", persona=name)

    assert f"# Persona: {persona.name}" in prompt
    persona_at = prompt.index(f"# Persona: {persona.name}")
    for rule in (
        "主人自己交来的凭据 + 明确指令 = 授权本身",
        "严禁编造工具返回值",
        "`fs.pull` 推到主机的",
        "默认是联网的",
    ):
        assert rule in prompt, f"{name} dropped {rule!r}"
        assert prompt.index(rule) > persona_at, f"{name} outranks {rule!r}"
    # the base contract is still there, and so is the sandbox reality
    assert "You are an autonomous engineering agent." in prompt
    assert "/workspace" in prompt


def test_fs_pull_is_in_the_seeded_core_tools():
    """The prompt tells the model to use fs.pull, so the seed must actually ship it."""
    from agent.ai.pull import PULL_HANDLERS
    from agent.registry.seed import CORE_TOOLS

    spec = next(item for item in CORE_TOOLS if item["name"] == "fs.pull")
    assert spec["executor"] == "host_native"
    assert "fs.pull" in PULL_HANDLERS
    assert "var\\pulled" in spec["description"]
