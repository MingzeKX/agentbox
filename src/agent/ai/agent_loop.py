"""The agent loop: resident tools only, tool results fed back until the model stops."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.ai import personas
from agent.ai.history import HISTORY_MESSAGE_MAX_CHARS, drop_leading_orphans, for_history, truncate_for_history
from agent.ai.llm import LLMClient, LLMError
from agent.ai.metacalls import MetaTools
from agent.config import settings
from agent.registry import repository as repo

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"
MAX_TOOL_CALLS_PER_STEP = 8

EventSink = Callable[[str, dict[str, Any]], None]


@dataclass
class Step:
    index: int
    tool: str
    arguments: dict[str, Any]
    ok: bool
    duration_ms: int = 0
    error: str | None = None
    preview: str = ""
    version: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "tool": self.tool,
            "arguments": self.arguments,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "preview": self.preview,
            "version": self.version,
        }


@dataclass
class TurnResult:
    content: str
    steps: list[Step] = field(default_factory=list)
    stop_reason: str = "stop"
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    tokens_estimate: int = 0


def load_system_prompt(session_id: str, extra: str | None = None, persona: str | None = None) -> str:
    """Base prompt + the configured persona + runtime facts.

    The persona is chosen by the operator (config or console), never by the model, and
    it is appended *after* the base contract, so it can restyle the answer but cannot
    remove the safety rules.
    """
    base = PROMPT_PATH.read_text(encoding="utf-8")
    facts = [f"Current session id: {session_id}.", "Your sandbox workspace persists for this session."]
    if extra:
        facts.append(extra)
    return personas.build_system_prompt(base, personas.load(persona), facts)


def _preview(value: Any, limit: int = 400) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:limit] + ("..." if len(text) > limit else "")


class AgentLoop:
    def __init__(
        self,
        *,
        session_id: str,
        llm: LLMClient,
        meta: MetaTools,
        sessionmaker: async_sessionmaker[AsyncSession],
        on_event: EventSink | None = None,
        max_steps: int | None = None,
        preview_chars: int = 400,
    ) -> None:
        self.session_id = session_id
        self.llm = llm
        self.meta = meta
        # how much of each tool result travels back to the client in events
        self.preview_chars = max(200, min(20_000, int(preview_chars)))
        self.sessionmaker = sessionmaker
        self.on_event = on_event
        self.max_steps = max_steps or settings.max_steps

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, payload)
            except Exception as exc:  # noqa: BLE001 - events must never break the loop
                log.debug("event sink failed: %s", exc)

    async def history(self) -> list[dict[str, Any]]:
        async with self.sessionmaker() as session:
            rows = await repo.load_messages(session, self.session_id, limit=settings.max_history_messages)
        # newest-first window -> a boundary can split a tool_call/tool result pair; the
        # window must never start with the `tool` half of one
        return drop_leading_orphans([row.content for row in rows if isinstance(row.content, dict)])

    async def _persist(self, role: str, message: dict[str, Any]) -> None:
        async with self.sessionmaker() as session:
            await repo.ensure_session(session, self.session_id)
            # stored in its *storable* form: text only and capped, so a screenshot or a
            # 3 MB `pip install` transcript can never be replayed on every later turn
            await repo.append_message(session, self.session_id, role, for_history(message))
            await session.commit()

    async def run(
        self, user_message: str | list[dict[str, Any]], system_extra: str | None = None
    ) -> TurnResult:
        started = time.monotonic()
        history = await self.history()
        user_msg = {"role": "user", "content": user_message}
        await self._persist("user", user_msg)

        messages: list[dict[str, Any]] = [{"role": "system", "content": load_system_prompt(self.session_id, system_extra)}]
        messages.extend(history)
        messages.append(user_msg)

        steps: list[Step] = []
        self.emit("start", {"session_id": self.session_id, "model": self.llm.model, "history": len(history)})

        final_content = ""
        stop_reason = "max_steps"
        usage: dict[str, Any] = {}
        for step_index in range(1, self.max_steps + 1):
            try:
                completion = await self.llm.chat(
                    messages,
                    tools=self.meta.specs,
                    on_token=lambda text: self.emit("token", {"text": text}),
                )
            except LLMError as exc:
                # the gateway's own explanation (truncated) travels with the error: a bare
                # "HTTP 400" told the operator nothing about which field was rejected
                self.emit("error", {"message": exc.detail(), "status": exc.status})
                stop_reason = "llm_error"
                final_content = f"LLM call failed: {exc.detail()}"
                break

            usage = completion.usage or usage
            calls = completion.tool_calls[:MAX_TOOL_CALLS_PER_STEP]
            if not calls:
                final_content = completion.content.strip()
                stop_reason = "stop" if final_content else "empty"
                assistant_msg = {
                    "role": "assistant",
                    "content": truncate_for_history(completion.content, HISTORY_MESSAGE_MAX_CHARS),
                }
                await self._persist("assistant", assistant_msg)
                messages.append(assistant_msg)
                break

            assistant_msg = {
                "role": "assistant",
                "content": truncate_for_history(completion.content, HISTORY_MESSAGE_MAX_CHARS) or "",
                "tool_calls": [call.to_message_part() for call in calls],
            }
            await self._persist("assistant", assistant_msg)
            messages.append(assistant_msg)

            for call in calls:
                self.emit(
                    "tool_call",
                    {"step": step_index, "tool": call.name, "arguments": call.arguments, "id": call.id},
                )
                outcome = await self.meta.call(call.name, call.arguments)
                step = Step(
                    index=step_index,
                    tool=call.name,
                    arguments=call.arguments,
                    ok=outcome.ok,
                    duration_ms=outcome.duration_ms,
                    error=outcome.error,
                    preview=_preview(outcome.result if outcome.ok else {"error": outcome.error}, self.preview_chars),
                    version=outcome.version,
                )
                steps.append(step)
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    # capped with a head+tail note: this row is replayed on EVERY later
                    # turn, so an unbounded result here is a session-wide problem, not a
                    # display problem
                    "content": truncate_for_history(
                        json.dumps(outcome.as_message(), ensure_ascii=False, default=str)
                    ),
                }
                await self._persist("tool", tool_msg)
                messages.append(tool_msg)
                self.emit("tool_result", step.as_dict())
        else:
            final_content = (
                f"I stopped after {self.max_steps} steps without reaching a conclusion. "
                "Partial results are in the tool trace above."
            )
            stop_reason = "max_steps"

        result = TurnResult(
            content=final_content,
            steps=steps,
            stop_reason=stop_reason,
            usage=usage,
            model=self.llm.model,
            tokens_estimate=int(usage.get("total_tokens") or 0),
        )
        self.emit(
            "done",
            {
                "content": result.content,
                "stop_reason": stop_reason,
                "steps": [step.as_dict() for step in steps],
                "duration_ms": int((time.monotonic() - started) * 1000),
                "usage": usage,
            },
        )
        return result
