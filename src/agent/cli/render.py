"""IDE-style rendering of an agent turn.

The plain view (one dim line per tool call) is fine for scripting but unreadable when
the agent takes ten steps.  This renders the same event stream the way an IDE renders
a build/run panel:

    1 │ ⏺ search_tools  {"query": "run a shell command"}                     ok · 0 ms
      │   ├ count: 5
      │   └ tools: [exec.run, fs.read, ...]
    2 │ ⏺ exec.run  {"argv": ["uname", "-a"]}                              ok · 52 ms
      │   stdout
      │     Linux sandbox-vm-528012c6c 6.12.107+deb13-amd64 ...
      │   exit_code: 0

Styles: ``ide`` (default), ``plain`` (the old one-liners), ``json`` (one JSON object
per line, for piping into jq).  ``json`` is a machine format and is never folded;
folding only ever touches the human views.

Folding
-------
A verbose tool result (``apt-get update``, ``ls -la /usr/bin``) buries the rest of the
turn.  With folding on (the default for the human styles) a long result shows only its
first ``FOLD_PREVIEW`` lines plus one summary line, and the full text stays retrievable
for a while:

  * ``/log fold off`` (or ``--no-log-fold``) prints whole results again
  * ``/more`` reprints the newest retained result, ``/more 2`` the one before it

Retention is bounded on purpose -- at most :data:`HISTORY_MAX_RECORDS` results and
:data:`HISTORY_MAX_CHARS` characters of text -- so a long session cannot grow forever.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console, ConsoleOptions, Group
from rich.panel import Panel
from rich.segment import Segment
from rich.syntax import Syntax
from rich.text import Text

GUTTER_WIDTH = 4
ICON_TOOL = "⏺"
ICON_OK = "✔"
ICON_FAIL = "✖"

#: prefix every answer line owns, so the transcript reads top-to-bottom: tool log, answer
ANSWER_PREFIX = "agent ›"
#: where the answer text starts: the prefix, plus the space that separates it from the text
ANSWER_INDENT = " " * (len(ANSWER_PREFIX) + 1)

#: how many characters of streamed answer text are buffered before it is printed
ANSWER_FLUSH_CHARS = 4000

#: how many output lines a folded result keeps before the summary line
FOLD_PREVIEW = 6
#: how many recent results /more can reach
HISTORY_MAX_RECORDS = 5
#: total text (/more reach + line reprints) kept in memory for those results
HISTORY_MAX_CHARS = 200_000

#: errors that have an obvious operator fix, shown under the failure
ERROR_HINTS = {
    "net_disabled": "operator: enable with /net on (AGENT_NET_ENABLED=1 + AGENT_NET_ALLOW_HOSTS)",
    "net_denied": "operator: add the host with /net allow <host>",
    "not_found": "the tool is not registered; use search_tools first",
    "invalid_args": "fetch the schema with get_tool_schema before calling",
    "timeout": "raise timeout_s or memory_mb for this call",
}

#: the smallest run of identical consecutive output lines worth collapsing as noise
#: (two identical lines are still plausible real output; three is a probing loop)
DUP_RUN_MIN = 3


@dataclass
class _Indent:
    """Indent a renderable by ``size`` columns *without* padding its lines to the width.

    ``rich.padding.Padding`` renders its child with ``pad=True``, which fills every line
    out to the terminal width so a background style can cover it.  On a real terminal that
    is visible as output padded with trailing spaces, which survives select/copy and makes
    the transcript look ragged, so the tool body is indented with this instead.
    """

    renderable: Any
    size: int

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> Iterable[Segment]:
        prefix = Segment(" " * self.size)
        child = options.update_width(max(1, options.max_width - self.size))
        for line in console.render_lines(self.renderable, child, pad=False):
            yield prefix
            yield from line
            yield Segment.line()


def _gutter(index: int | None, last: bool = False) -> Text:
    if index is None:
        prefix = " " * GUTTER_WIDTH
    else:
        prefix = f"{index:>{GUTTER_WIDTH - 2}} " + ("└" if last else "│") + " "
    return Text(prefix, style="dim")


def _clip(text: str, lines: int) -> tuple[str, int]:
    rows = text.splitlines()
    if len(rows) <= lines:
        return text, 0
    return "\n".join(rows[:lines]), len(rows) - lines


def _count(count: int) -> str:
    """Group thousands: 4-digit line counts stay readable."""
    return f"{count:,}"


def _fold_note(hidden: int, total: int) -> str:
    """The one line that replaces everything folding hid."""
    return f"… 已折叠 {_count(hidden)} 行（共 {_count(total)} 行 · /more 看全文 · /log fold off 关闭折叠）"


def wrap_answer(text: str, width: int, *, after_prefix: bool = True) -> str:
    """Wrap ``text`` to ``width`` columns, keeping its paragraph/list/code structure.

    Blank lines stay blank (that is what separates paragraphs, keeps a list a list and
    keeps a ``` fence a fence) and every line after the first is indented by
    :data:`ANSWER_INDENT` so a wrapped answer keeps one straight left edge instead of
    drifting.

    ``after_prefix=True`` is the streaming case: the caller has already written the
    ``agent ›`` gutter on the current line, so the first row starts immediately after it.
    ``after_prefix=False`` indents every row, for the ``/more answer`` reprint where the
    gutter has a line to itself.

    A trailing newline in ``text`` is preserved, because the renderer flushes one line at
    a time: without it every streamed line would be glued onto the previous one (which is
    what used to turn a fenced code block into ``\\`\\`\\`Linux … \\`\\`\\``` on a single line).
    """
    limit = max(20, int(width))
    source = str(text or "")
    rows: list[str] = []
    for line in source.splitlines():
        stripped = line.rstrip()
        if not stripped:
            rows.append("")  # stays genuinely empty: no gutter spaces on a blank line
            continue
        indent = " " * (len(stripped) - len(stripped.lstrip()))
        parts = (
            textwrap.wrap(
                stripped.lstrip(),
                width=max(20, limit - len(indent)),
                break_long_words=True,
                break_on_hyphens=False,
                replace_whitespace=False,
                drop_whitespace=True,
            )
            or [""]
        )
        rows.append(indent + parts[0])
        rows.extend(indent + part for part in parts[1:])

    body: list[str] = []
    for position, row in enumerate(rows):
        if not row or (position == 0 and after_prefix):
            body.append(row)
        else:
            body.append(ANSWER_INDENT + row)
    wrapped = "\n".join(body)
    if source.endswith("\n"):
        wrapped += "\n"
    return wrapped


def collapse_repeats(text: str, *, minimum: int = DUP_RUN_MIN) -> str:
    """Collapse a run of identical consecutive lines into one line plus a count.

    The guest is routinely noisy in a way that buries the real output: a command that
    probes several cgroup knobs prints the *same* ``… Permission denied`` line once per
    knob, and one of those runs can fill the whole preview.  The count is kept so nothing
    is silently lost, and ``/more`` still reprints the untouched text.
    """
    rows = str(text or "").splitlines()
    out: list[str] = []
    index = 0
    while index < len(rows):
        row = rows[index]
        run = 1
        while index + run < len(rows) and rows[index + run] == row:
            run += 1
        if run >= minimum and row.strip():
            out.append(row)
            out.append(f"… 上一行重复了 {run - 1} 次（共 {run} 行相同 · /more 看全文）")
        else:
            out.extend(rows[index : index + run])
        index += run
    return "\n".join(out)


@dataclass
class ResultRecord:
    """One retained tool result, enough for ``/more`` to reprint it."""

    index: int
    tool: str
    label: str
    text: str
    stderr: str = ""
    meta: str = ""
    ok: bool = True
    error: str = ""
    duration_ms: int = 0


def _compact_args(args: dict[str, Any], width: int = 96) -> str:
    try:
        text = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(args)
    return text if len(text) <= width else text[: width - 1] + "…"


def _pick(payload: dict[str, Any]) -> tuple[str, str]:
    """(label, body) for a result payload, favouring what a human wants to read."""
    for key in ("stdout", "text", "result_preview", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return key, value
    if isinstance(payload.get("result"), dict):
        return "result", json.dumps(payload["result"], ensure_ascii=False, indent=2, default=str)
    return "result", json.dumps(payload, ensure_ascii=False, indent=2, default=str)


@dataclass
class StepRenderer:
    """Event sink that draws the turn like an IDE panel."""

    console: Console
    style: str = "ide"
    max_lines: int = 24
    show_args: bool = True
    fold: bool = True
    fold_preview: int = FOLD_PREVIEW
    _step: int = 0
    _step_tools: dict[str, int] = field(default_factory=dict)
    _streaming: bool = False
    _failures: int = 0
    _last_tool: str = ""
    _results: list[ResultRecord] = field(default_factory=list)
    _result_chars: int = 0
    #: the answer is buffered and printed as one wrapped block: text that streams into the
    #: middle of a tool-log line is exactly the mess this fixes
    _answer_buffer: str = ""
    _answer_prefix_written: bool = False
    _answer_chars: int = 0
    _answer_open: bool = False
    _last_answer: str = ""
    #: the ``agent ›`` line already carries answer text, so later chunks are continuations
    _answer_started: bool = False
    #: the console is not at the start of a line (so a terminator is still needed)
    _answer_line_open: bool = False
    #: something of this turn is already on screen, and a blank line was already emitted
    _any_output: bool = False
    _blank_open: bool = False
    #: called with text that is about to be printed; returns how many sandbox images it
    #: painted (the console's ``show_inline_images``).  None = no inline images at all.
    image_hook: Any = None
    #: images one turn may paint inline; the hook caps each call, this caps the whole turn
    image_limit: int = 2
    #: images painted since this turn's ``done`` event (reset there, so the cap is per turn)
    _inline_images: int = 0

    # ------------------------------------------------------------------ history
    def _remember(self, record: ResultRecord) -> None:
        """Retain a result for /more, bounded by count and total characters."""
        size = len(record.text) + len(record.stderr)
        self._results.append(record)
        self._result_chars += size
        while self._results and (
            len(self._results) > HISTORY_MAX_RECORDS
            or (self._result_chars > HISTORY_MAX_CHARS and len(self._results) > 1)
        ):
            dropped = self._results.pop(0)
            self._result_chars -= len(dropped.text) + len(dropped.stderr)

    @property
    def retained(self) -> int:
        """How many results /more can currently reach."""
        return len(self._results)

    def full_output(self, back: int = 1) -> ResultRecord | None:
        """The ``back``-th newest retained result (1 = the last one), or None."""
        if back < 1 or back > len(self._results):
            return None
        return self._results[-back]

    # ------------------------------------------------------------------ helpers
    def _end_stream(self) -> None:
        if self._streaming:
            self.console.file.write("\n")
            self.console.file.flush()
            self._streaming = False

    def _blank(self) -> None:
        """Start a new block: exactly one blank line, never two and never a leading one.

        The transcript is a sequence of blocks (the ``you ›`` line, the tool log, the
        ``agent ›`` answer, the footer), and every pair of them is separated by exactly one
        blank line.  Tracking it here is what stops the old behaviour of two blank lines in
        one place and none in another.
        """
        if self._blank_open or not self._any_output:
            return
        self.console.file.write("\n")
        self.console.file.flush()
        self._blank_open = True

    def _printed(self) -> None:
        """Note that a block just wrote real content (so the next block gets its blank)."""
        self._any_output = True
        self._blank_open = False
        self._answer_line_open = False

    @property
    def answer(self) -> str:
        """The text of the last finished answer (``""`` before the first one)."""
        return self._last_answer

    # -------------------------------------------------------------- answer block
    def _answer_text(self, text: str) -> None:
        """Collect streamed answer text; it is printed wrapped, not mid-line.

        A finished line is printed as soon as it arrives, so a long answer still streams;
        a partial line waits for its newline (or the size bound) so wrapping happens on
        whole lines and never mid-word.
        """
        if not text:
            return
        self._answer_open = True
        self._answer_buffer += text
        self._answer_chars += len(text)
        if "\n" in self._answer_buffer or len(self._answer_buffer) >= ANSWER_FLUSH_CHARS:
            self._flush_answer()

    def _offer_images(self, text: str) -> None:
        """Offer text that is about to be printed to :attr:`image_hook`, if any.

        The hook (the console) pulls the sandbox images the text mentions and paints them,
        which is what puts a picture the agent produced straight into the transcript.  A
        turn stops after ``image_limit`` of them so a directory listing cannot turn the
        answer into a gallery, and a hook that fails is swallowed: the picture is a bonus,
        the text is the answer.
        """
        if self.image_hook is None or not text or self._inline_images >= self.image_limit:
            return
        if self._answer_line_open:
            # a picture starts on its own line: the answer's partial line is not a canvas
            self.console.file.write("\n")
            self.console.file.flush()
            self._answer_line_open = False
        try:
            self._inline_images += int(self.image_hook(text) or 0)
        except Exception:  # noqa: BLE001 - a picture must never break the turn
            return

    def _flush_answer(self) -> None:
        chunk, self._answer_buffer = self._answer_buffer, ""
        if not chunk:
            return
        if not self._answer_prefix_written:
            self._ensure_answer_prefix()
        # the first chunk continues the ``agent ›`` line; every later one is a continuation
        body = wrap_answer(chunk, self._answer_width(), after_prefix=not self._answer_started)
        self.console.file.write(body)
        self.console.file.flush()
        self._answer_started = True
        self._answer_line_open = not body.endswith("\n")
        self._any_output = True
        self._offer_images(chunk)

    def _answer_width(self) -> int:
        """Wrap to the terminal, minus the room the prefix/indent takes up."""
        width = int(getattr(self.console, "width", 0) or 0)
        if width <= 0:
            width = 100
        return max(20, width - len(ANSWER_INDENT))

    def _ensure_answer_prefix(self) -> None:
        """Put ``agent ›`` on its own gutter, never glued to the tool log above it."""
        self._end_stream()
        self._blank()
        # no newline: the first wrapped row belongs on this same line, so the continuation
        # indent lines up under the text instead of under a prefix on its own line
        self.console.print(f"[dim]{ANSWER_PREFIX} [/dim]", end="")
        self._answer_prefix_written = True
        self._any_output = True
        self._blank_open = False
        self._answer_line_open = True

    def finish_answer(self, final: bool = False, text: str | None = None) -> None:
        """Close the tool block and print the answer, wrapped, separated by one blank line.

        ``final`` is what the REPL calls at the end of a turn: it also records the answer
        (``text`` when the caller has it, else the streamed text) so ``/more answer`` can
        show it.  Calling this twice is harmless (nothing is buffered the second time).
        """
        self._end_stream()
        if text is not None:
            self._last_answer = str(text)
        if not self._answer_open and not self._answer_prefix_written and not self._answer_buffer and not final:
            return
        self._flush_answer()
        if self._answer_line_open:
            # terminate the answer's last line; already-terminated output must not gain a
            # second blank line (that was the "2 blank lines in places" defect)
            self.console.file.write("\n")
            self.console.file.flush()
            self._answer_line_open = False
        if final:
            self._remember_answer()
        self._answer_open = False
        self._answer_prefix_written = False
        self._answer_started = False

    def _remember_answer(self) -> None:
        """Keep the newest whole answer inside the same bound as the tool results."""
        text = self._last_answer
        while self._results and self._result_chars + len(text) > HISTORY_MAX_CHARS and len(self._results) > 1:
            dropped = self._results.pop(0)
            self._result_chars -= len(dropped.text) + len(dropped.stderr)

    def print_full_answer(self) -> bool:
        """Reprint the last answer in full (``/more answer``); False when there is none."""
        if not self._last_answer.strip():
            return False
        self.console.print(f"[dim]{ANSWER_PREFIX}[/dim] [dim]（上一条回答 · 完整文本）[/dim]")
        # the gutter has a line to itself here, so every row lines up under it
        self.console.print(wrap_answer(self._last_answer, self._answer_width(), after_prefix=False))
        self.console.print()
        return True

    # ------------------------------------------------------------------- events
    def handle(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if self.style == "json":
            # raw write, not console.print: rich would wrap long lines and break the
            # "one JSON object per line" contract needed for piping into jq
            if kind != "token":  # tokens would flood the pipe
                self.console.file.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                self.console.file.flush()
            return
        if self.style == "plain":
            self._plain(event)
            return
        if kind == "token":
            self._answer_text(str(event.get("text") or ""))
            return
        if kind == "tool_call":
            self.finish_answer()
            self._tool_call(event)
        elif kind == "tool_result":
            self.finish_answer()
            self._tool_result(event)
        elif kind == "error":
            self.finish_answer()
            self.console.print(
                Panel(
                    Text(str(event.get("message", "")), style="red"),
                    title="[red]agent error[/red]",
                    border_style="red",
                    title_align="left",
                )
            )
            self._printed()
        elif kind == "done":
            self._done(event)

    # ------------------------------------------------------------------- styles
    def _plain(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "token":
            # same hygiene as the ide view: buffered, prefixed and wrapped, never glued to
            # the previous tool log line
            self._answer_text(str(event.get("text") or ""))
        elif kind == "tool_call":
            self.finish_answer()
            args = json.dumps(event.get("arguments", {}), ensure_ascii=False)
            self.console.print(f"[bold yellow]→ {event.get('tool')}[/bold yellow] [dim]{args[:400]}[/dim]")
            self._printed()
        elif kind == "tool_result":
            self.finish_answer()
            status = "[green]ok[/green]" if event.get("ok") else "[red]failed[/red]"
            detail = event.get("preview") or event.get("error") or ""
            self.console.print(f"  [dim]{status} {event.get('duration_ms', 0)}ms[/dim] {detail[:400]}")
            # the untruncated preview: an image path is usually past the 400 char cut
            self._offer_images(str(event.get("preview") or ""))
            self._printed()
        elif kind == "error":
            self.finish_answer()
            self.console.print(f"[bold red]error:[/bold red] {event.get('message')}")
        elif kind == "done":
            self._done(event)

    def _tool_call(self, event: dict[str, Any]) -> None:
        self._end_stream()
        self._step = int(event.get("index") or self._step + 1)
        tool = str(event.get("tool") or "?")
        self._last_tool = tool
        self._step_tools[tool] = self._step_tools.get(tool, 0) + 1
        header = Text()
        header.append_text(_gutter(self._step))
        header.append(f"{ICON_TOOL} {tool}", style="bold magenta")
        if self.show_args:
            header.append("  " + _compact_args(event.get("arguments") or {}), style="dim")
        self.console.print(header, soft_wrap=True)
        self._printed()

    def _captions(self, label: str, text: str, stderr: str) -> None:
        """The labelled, gutter-owned body of a result (used by the live view and /more)."""
        if label in {"result"} and text.lstrip().startswith(("{", "[")):
            renderable: Any = Syntax(text, "json", background_color="default", word_wrap=True)
        else:
            renderable = Syntax(text, "text", background_color="default", word_wrap=True)
        caption = Text()
        caption.append_text(_gutter(None))
        caption.append(f"  {label}", style="bold cyan")
        self.console.print(caption)
        # indent the body so the step visually owns it, like an IDE output panel.
        # _Indent, not Padding: Padding pads every line out to the terminal width with
        # spaces, which is visible as ragged trailing whitespace on a real terminal.
        self.console.print(_Indent(renderable, GUTTER_WIDTH + 2))
        if stderr:
            err_caption = Text()
            err_caption.append_text(_gutter(None))
            err_caption.append("  stderr", style="bold red")
            self.console.print(err_caption)
            self.console.print(_Indent(Syntax(stderr, "text", background_color="default"), GUTTER_WIDTH + 2))

    def _tool_result(self, event: dict[str, Any]) -> None:
        ok = bool(event.get("ok"))
        duration = event.get("duration_ms", 0)
        body = Text()
        body.append_text(_gutter(None))
        body.append(f"  {ICON_OK if ok else ICON_FAIL} ", style="green" if ok else "red")
        body.append(f"{duration} ms", style="dim")
        if event.get("version"):
            body.append(f" · v{event['version']}", style="dim")
        self.console.print(body)

        payload: dict[str, Any] = {}
        raw_preview = event.get("preview") or ""
        if raw_preview:
            try:
                parsed = json.loads(raw_preview)
                payload = parsed if isinstance(parsed, dict) else {"result": parsed}
            except json.JSONDecodeError:
                payload = {"result": raw_preview}

        if not ok:
            self._failures += 1
            message = str(event.get("error") or payload.get("error") or "failed")
            hint = ERROR_HINTS.get(str(payload.get("error_code") or ""))
            lines = [Text(f"  {message}", style="red")]
            if hint:
                lines.append(Text(f"  {hint}", style="yellow"))
            self.console.print(Panel(Group(*lines), border_style="red", title_align="left", title="[red]failed[/red]"))
            self._remember(
                ResultRecord(
                    index=int(event.get("index") or self._step),
                    tool=str(event.get("tool") or self._last_tool),
                    label="error",
                    text=message,
                    ok=False,
                    error=message,
                    duration_ms=int(duration or 0),
                )
            )
            self._printed()
            return

        label, text = _pick(payload)
        text = text.rstrip()
        stderr = payload.get("stderr") if isinstance(payload.get("stderr"), str) else ""
        stderr = (stderr or "").rstrip()
        # a compact meta line first: exit codes and byte counts are what you scan for
        meta = self._meta_line(payload, skip=label)
        if meta:
            line = Text()
            line.append_text(_gutter(None))
            line.append("  " + meta, style="dim")
            self.console.print(line)

        rows = text.splitlines()
        # the routine guest noise (one identical "… Permission denied" line per cgroup knob
        # it tried) is collapsed for the preview only: ResultRecord keeps the raw text, so
        # /more still reprints every line
        if self.fold and len(rows) > self.fold_preview:
            # folded: the header/meta stay, only the head of the output is printed
            shown, hidden = _clip(text, self.fold_preview)
            self._captions(label, collapse_repeats(shown), stderr)
            note = Text()
            note.append_text(_gutter(None))
            note.append("  " + _fold_note(hidden, len(rows)), style="yellow")
            self.console.print(note)
        else:
            clipped, extra = _clip(text, self.max_lines)
            self._captions(label, collapse_repeats(clipped), stderr)
            if extra:
                note = Text()
                note.append_text(_gutter(None))
                note.append(
                    f"  … +{extra} more line(s) — raise --log-lines or use /more to see them", style="dim"
                )
                self.console.print(note)
        self._remember(
            ResultRecord(
                index=int(event.get("index") or self._step),
                tool=str(event.get("tool") or self._last_tool),
                label=label,
                text=text,
                stderr=stderr,
                meta=meta,
                ok=ok,
                duration_ms=int(duration or 0),
            )
        )
        # the whole result, not the folded preview: a path below the fold still counts
        self._offer_images(text)
        self._printed()

    def print_full_output(self, back: int = 1) -> bool:
        """Reprint a retained result in full; False when nothing that old is retained.

        Used by ``/more``.  The json style is a machine format and is never consulted,
        but ``/more`` still works there because this reads the in-memory records.
        """
        record = self.full_output(back)
        if record is None:
            return False
        header = Text()
        header.append_text(_gutter(record.index))
        header.append(f"{ICON_TOOL} {record.tool}", style="bold magenta")
        header.append(f"  （第 {back} 近的结果 · 完整输出）", style="dim")
        self.console.print(header)
        status = Text()
        status.append_text(_gutter(None))
        status.append(f"  {ICON_OK if record.ok else ICON_FAIL} ", style="green" if record.ok else "red")
        status.append(f"{record.duration_ms} ms", style="dim")
        self.console.print(status)
        if record.meta:
            line = Text()
            line.append_text(_gutter(None))
            line.append("  " + record.meta, style="dim")
            self.console.print(line)
        self._captions(record.label, record.text, record.stderr)
        return True

    @staticmethod
    def _meta_line(payload: dict[str, Any], skip: str) -> str:
        wanted = (
            "exit_code",
            "status_code",
            "bytes_written",
            "bytes",
            "saved",
            "timed_out",
            "truncated",
            "oom",
            "killed_by_limit",
            "signal",
            "path",
        )
        parts: list[str] = []
        for key in wanted:
            if key == skip or key not in payload:
                continue
            value = payload[key]
            if value in (None, False, "") and key not in {"exit_code", "status_code", "truncated"}:
                continue
            if isinstance(value, dict):
                value = value.get("path") or value.get("bytes") or value
            parts.append(f"{key}={value}")
        return " · ".join(parts)

    def _done(self, event: dict[str, Any]) -> None:
        self.finish_answer()
        self._blank()  # one blank line between the answer/tool block and the footer
        self.console.print(Text(self._summary(event), style="dim"))
        if self._step_tools:
            used = ", ".join(f"{name}×{count}" for name, count in sorted(self._step_tools.items()))
            self.console.print(Text(f"tools: {used}", style="dim"))
        self._printed()
        # the turn is over: the next one gets its own inline-image budget
        self._inline_images = 0

    def _summary(self, event: dict[str, Any]) -> str:
        steps = event.get("steps") or []
        usage = event.get("usage") or {}
        turns = len(steps)
        parts = [f"{turns} tool call(s)", f"{event.get('duration_ms', 0)} ms"]
        if self._step != turns:
            # _step counts every call this *session* has made (this renderer survives
            # between turns), which is why two failed turns printed the same number: without
            # this label the count reads as "what just happened" and misleads everyone
            parts.append(f"本会话累计 {self._step} 次")
        if usage.get("total_tokens"):
            parts.append(f"{usage['total_tokens']} tokens")
        if self._failures:
            parts.append(f"{self._failures} failed")
        parts.append(f"stop={event.get('stop_reason')}")
        return " · ".join(parts)


def make_renderer(
    console: Console, style: str, log_lines: int, show_args: bool = True, fold: bool = True
) -> StepRenderer:
    return StepRenderer(console=console, style=style, max_lines=log_lines, show_args=show_args, fold=fold)
