"""What may travel in a request's ``messages``: the history budget and its sanitation.

Why this module exists
----------------------
A session used to be *poisonable*.  One oversized tool result -- a ``pip install``
transcript, a ``toolsmith.create`` echo of a whole source file, a multi-megabyte
``net.fetch`` body -- was stored verbatim in ``messages`` and then replayed on **every**
later turn.  The gateway rejected the request body with HTTP 400 in ~200 ms, before the
model ever ran, and because the offending row stayed inside the history window every
following turn failed the same way until the operator started a new session (``/new``).

The same thing happened with a *small* row: :func:`agent.registry.repository.load_messages`
keeps the newest ``max_history_messages`` rows, so the window boundary can fall between an
``assistant`` message that declares ``tool_calls`` and the ``tool`` messages that answer
them.  A ``role: tool`` message whose ``tool_call_id`` no assistant message declares is
invalid for the API, and it poisons the session exactly as permanently.

So there are two independent guards, deliberately in two different places:

* **ingest** -- :func:`truncate_for_history` / :func:`for_history` cap what a single turn
  may add to the stored history (head + tail + a note saying how much was dropped);
* **egress** -- :func:`sanitize_history` re-checks *every* outbound request, because a row
  written by an older version (or by hand) must not be able to break a session either.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

#: how many characters of a single tool result may enter the session history.  The model
#: never needs a multi-megabyte transcript, and once such a row is stored it is replayed
#: on every later turn, so this is a hard cap rather than a display preference.
HISTORY_TOOL_MAX_CHARS = 12_000

#: how many characters one user/assistant message may enter the history with
HISTORY_MESSAGE_MAX_CHARS = 24_000

#: total size of the ``messages`` array we are willing to send, in characters
HISTORY_MAX_CHARS = 400_000

#: what replaces the middle of an over-long value
HISTORY_TRUNCATION_NOTE = "\n…（已截断 {dropped} 字符，完整内容见 /more 或工作区文件）\n"

#: how much of the gateway's own error body is shown to the operator
GATEWAY_BODY_MAX_CHARS = 600


def truncate_for_history(text: str, limit: int = HISTORY_TOOL_MAX_CHARS) -> str:
    """Cap ``text`` to ``limit`` characters, keeping the head, the tail and a note.

    Head *and* tail because both ends carry signal: a command echoes what it was asked to
    do and then prints why it failed.  Cutting only the tail would keep the invocation and
    lose the error.
    """
    value = str(text or "")
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    note = HISTORY_TRUNCATION_NOTE.format(dropped=len(value) - limit)
    if len(note) >= limit:
        return value[:limit]
    keep = limit - len(note)
    head = keep * 2 // 3
    tail = keep - head
    return value[:head] + note + value[len(value) - tail :]


def _text_of(content: Any) -> str:
    """The plain text of a message content, dropping anything that is not text.

    ``content`` is normally a string, but an image turn stores a parts list
    (``[{"type": "text"}, {"type": "image_url", ...}]``).  The text parts are what the
    model can still use later; the image part is a base64 data URL that must never be
    replayed (see :func:`for_history`).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in (None, "text", "input_text"):
                texts.append(str(part.get("text") or ""))
        return "\n".join(item for item in texts if item)
    if content is None:
        return ""
    return str(content)


def _image_count(content: Any) -> int:
    if not isinstance(content, list):
        return 0
    return sum(1 for part in content if isinstance(part, dict) and part.get("type") == "image_url")


def for_history(message: dict[str, Any]) -> dict[str, Any]:
    """The *storable* form of a message: text only, capped.

    An image travels inline as a ``data:`` URL.  Storing it would put megabytes into the
    session history and replay them on every later turn -- which both bloats the request
    and re-asks a text model to read an image -- so only a short placeholder is kept.  The
    turn that carried the picture still sends the real parts; history does not.
    """
    stored = dict(message)
    content = message.get("content")
    if isinstance(content, list):
        images = _image_count(content)
        text = _text_of(content)
        prefix = f"[图片 ×{images}] " if images else ""
        content = (prefix + text).strip()
    stored["content"] = truncate_for_history(content, HISTORY_MESSAGE_MAX_CHARS)
    return stored


def _clean_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """Only well-formed function calls survive: a malformed one is a 400 from the gateway."""
    calls: list[dict[str, Any]] = []
    for call in raw or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict) or not function.get("name"):
            continue
        call_id = str(call.get("id") or "")
        if not call_id:
            continue
        calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": str(function["name"]),
                    "arguments": str(function.get("arguments") or "{}"),
                },
            }
        )
    return calls


def _size(entry: dict[str, Any]) -> int:
    try:
        return len(json.dumps(entry, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(entry))


def drop_leading_orphans(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim a history window so it never *begins* with a ``tool`` message.

    :func:`agent.registry.repository.load_messages` keeps the newest rows, so the window
    boundary lands between an ``assistant`` message that declares ``tool_calls`` and the
    ``tool`` messages that answer them -- often, because every step of the agent loop adds
    one assistant plus one tool row per call.  The leading ``tool`` messages then name a
    ``tool_call_id`` that no message in the window declares, which the gateway rejects.

    The partial group is dropped ("rather one message fewer than an invalid request"), and
    the window starts at a message the model can read on its own.  This is the fix for the
    *cause*; :func:`sanitize_history` stays as the per-request safety net for rows written
    by an older version.
    """
    start = 0
    while start < len(messages) and str(messages[start].get("role") or "") == "tool":
        start += 1
    if start:
        log.info("history window: dropped %d leading orphaned tool message(s)", start)
    return messages[start:]


def _prune_unanswered(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enforce the gateway's *ordering* rule on ``entries``.

    A declaration is only valid when the ``tool`` messages that answer it follow the
    assistant message **immediately**: the API rejects a request in which an assistant
    message with ``tool_calls`` is not directly followed by one ``tool`` message per
    declared id -- "insufficient tool messages following tool_calls message".  Matching the
    ids anywhere in the window is not enough, and that is the hole this closes: on
    2026-10-05 a tool that timed out after 60 s had its failure row written twelve rows
    after the declaration that asked for it (two turns of one session interleaved, because
    the streaming endpoint starts a task per request), behind a ``user`` message and five
    more steps -- every id matched, the order was still fatal.

    So the pairing is resolved positionally, in one forward pass: an assistant message
    opens a group, the ``tool`` messages directly after it answer it, and anything else
    closes the group.  Calls still unanswered when the group closes lose their entry in the
    declaration (and the message itself if that leaves it empty); a ``tool`` message that
    arrives outside its group is an orphan and goes.  Both sides of the pair always move
    together, so no id is ever half-answered.
    """
    pruned: list[dict[str, Any]] = []
    open_calls: dict[str, dict[str, Any]] = {}
    answered: set[str] = set()

    def close_group() -> None:
        for call_id, entry in open_calls.items():
            if call_id in answered:
                continue
            remaining = [call for call in entry.get("tool_calls", []) if call["id"] != call_id]
            if remaining:
                entry["tool_calls"] = remaining
            else:
                # no empty array either: the key is simply absent, like a plain message
                entry.pop("tool_calls", None)
        open_calls.clear()
        answered.clear()

    for entry in entries:
        role = str(entry.get("role") or "")
        if role == "tool":
            call_id = str(entry.get("tool_call_id") or "")
            if call_id in open_calls and call_id not in answered:
                answered.add(call_id)
                pruned.append(entry)
            else:
                log.debug("dropping orphaned tool message (tool_call_id=%s)", call_id or "<empty>")
            continue
        close_group()
        pruned.append(entry)
        if role == "assistant":
            for call in entry.get("tool_calls") or []:
                open_calls[call["id"]] = entry
    close_group()

    return [
        entry
        for entry in pruned
        if entry["role"] != "assistant" or entry.get("tool_calls") or str(entry.get("content") or "").strip()
    ]


def sanitize_history(
    messages: list[dict[str, Any]],
    *,
    max_chars: int = HISTORY_MAX_CHARS,
    tool_max_chars: int = HISTORY_TOOL_MAX_CHARS,
) -> list[dict[str, Any]]:
    """Return a request-safe copy of ``messages``.

    Everything that makes a request body invalid or unbounded is dealt with here, so no
    caller has to remember to:

    * every ``tool_calls``/``tool`` pair is resolved *positionally* by
      :func:`_prune_unanswered` -- a declaration whose answers are missing, out of order or
      cut off by the budget boundary loses those entries, and the orphaned ``tool``
      messages go with them;
    * empty/whitespace messages are dropped;
    * ``image_url`` parts are dropped from every message except the last one, which is the
      turn being sent (history keeps only the placeholder text);
    * every tool result and message is capped, and the whole array is trimmed from its
      oldest end to ``max_chars``, with the system prompt and the current turn always kept.
    """
    system: dict[str, Any] = {}
    entries: list[dict[str, Any]] = []
    last_index = len(messages) - 1

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "system":
            system = {"role": "system", "content": _text_of(message.get("content"))}
            continue
        if role == "user":
            text = _text_of(message.get("content"))
            if index == last_index and isinstance(message.get("content"), list):
                # the turn being sent keeps its real parts, so vision still works
                parts = [part for part in message["content"] if isinstance(part, dict)]
                if parts:
                    entries.append({"role": "user", "content": parts})
                    continue
            if not text.strip():
                continue
            entries.append({"role": "user", "content": truncate_for_history(text, HISTORY_MESSAGE_MAX_CHARS)})
            continue
        if role == "assistant":
            text = _text_of(message.get("content"))
            calls = _clean_tool_calls(message.get("tool_calls"))
            if not calls and not text.strip():
                continue
            entry: dict[str, Any] = {"role": "assistant", "content": text}
            if calls:
                entry["tool_calls"] = calls
            entries.append(entry)
            continue
        if role == "tool":
            entries.append(
                {
                    "role": "tool",
                    "tool_call_id": str(message.get("tool_call_id") or ""),
                    "name": str(message.get("name") or ""),
                    "content": truncate_for_history(_text_of(message.get("content")), tool_max_chars),
                }
            )
            continue

    # an assistant may only declare calls that the messages *directly after* it answer
    entries = _prune_unanswered(entries)

    # budget: keep the newest that fit, oldest first when sent
    kept: list[dict[str, Any]] = []
    total = 0
    for entry in reversed(entries):
        size = _size(entry)
        if kept and total + size > max_chars:
            break
        kept.append(entry)
        total += size
    kept.reverse()
    # the trim can cut a declaration off its answers (the boundary lands *inside* a group),
    # so the ordering rule has to hold for what actually goes out, not just for what came in
    kept = _prune_unanswered(kept)

    dropped = len(entries) - len(kept)
    if dropped and system:
        system["content"] = (
            f"{system.get('content') or ''}\n\n（更早的 {dropped} 条历史消息因超出上下文预算已省略）"
        ).strip()
    return ([system] if system else []) + kept
