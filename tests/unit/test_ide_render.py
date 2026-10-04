"""The IDE-style tool log.

Rendered with a recording Console so the assertions check what a user would actually
see, not an internal data structure.
"""

from __future__ import annotations

import io
import json

import pytest
from rich.console import Console

from agent.cli.render import (
    ANSWER_INDENT,
    ANSWER_PREFIX,
    GUTTER_WIDTH,
    StepRenderer,
    collapse_repeats,
    make_renderer,
    wrap_answer,
)


def renderer(style: str = "ide", lines: int = 24) -> tuple[StepRenderer, Console]:
    console = Console(record=True, width=120, force_terminal=False, no_color=True)
    return make_renderer(console, style, lines), console


def raw_renderer(width: int = 80, style: str = "ide", lines: int = 24) -> tuple[StepRenderer, io.StringIO]:
    """A renderer whose *raw* output is readable.

    The answer block and the tool body write through ``console.file``, so a recording
    Console would not see them; a StringIO file sees exactly what a terminal receives.
    """
    buf = io.StringIO()
    console = Console(file=buf, width=width, force_terminal=False, no_color=True, highlight=False)
    return make_renderer(console, style, lines), buf


def blank_runs(text: str) -> list[int]:
    """The lengths of every run of blank lines (1 is the intended separation)."""
    runs: list[int] = []
    count = 0
    for line in text.rstrip("\n").split("\n"):
        if line.strip():
            if count:
                runs.append(count)
            count = 0
        else:
            count += 1
    if count:
        runs.append(count)
    return runs


def stream_answer(r: StepRenderer, answer: str) -> None:
    """Feed an answer the way the SSE stream does: one finished line per token event."""
    for line in answer.splitlines(keepends=True):
        r.handle({"type": "token", "text": line})
    r.finish_answer(final=True, text=answer)


def long_body(lines: int = 48) -> str:
    return "\n".join(f"line {i}" for i in range(lines))


def result_event(body: str, ok: bool = True, **extra) -> dict:
    payload = {"stdout": body, **extra}
    return {
        "type": "tool_result",
        "index": 1,
        "tool": "exec.run",
        "ok": ok,
        "duration_ms": 10,
        "preview": json.dumps(payload),
    }


def sample_turn(r: StepRenderer) -> None:
    r.handle({"type": "tool_call", "index": 1, "tool": "exec.run", "arguments": {"argv": ["uname", "-a"]}})
    r.handle(
        {
            "type": "tool_result",
            "index": 1,
            "tool": "exec.run",
            "ok": True,
            "duration_ms": 52,
            "preview": json.dumps({"exit_code": 0, "stdout": "Linux sandbox-vm-1 6.12.107\n"}),
        }
    )
    r.handle(
        {
            "type": "done",
            "steps": [{"index": 1}],
            "duration_ms": 1200,
            "usage": {"total_tokens": 321},
            "stop_reason": "stop",
        }
    )


def test_ide_view_shows_a_gutter_tool_and_result():
    r, console = renderer()
    sample_turn(r)
    out = console.export_text()  # export_text() clears the buffer, so capture once
    assert "1 │" in out, out
    assert "exec.run" in out
    assert "uname" in out
    assert "52 ms" in out
    assert "Linux sandbox-vm-1" in out
    assert "exit_code" in out
    assert "1 tool call(s)" in out and "321 tokens" in out


def test_long_results_are_clipped_with_a_hint():
    r, console = renderer(lines=3)
    r.fold = False  # --no-log-fold: the historical / --log-lines behaviour
    r.handle({"type": "tool_call", "index": 1, "tool": "exec.run", "arguments": {}})
    body = "\n".join(f"line {i}" for i in range(50))
    r.handle(
        {
            "type": "tool_result",
            "index": 1,
            "tool": "exec.run",
            "ok": True,
            "duration_ms": 10,
            "preview": json.dumps({"stdout": body}),
        }
    )
    out = console.export_text()
    assert "line 2" in out
    assert "line 30" not in out
    assert "+47 more line(s)" in out
    assert "--log-lines" in out


def test_failures_are_panels_with_an_operator_hint():
    r, console = renderer()
    r.handle({"type": "tool_call", "index": 1, "tool": "net.fetch", "arguments": {"url": "https://x/"}})
    r.handle(
        {
            "type": "tool_result",
            "index": 1,
            "tool": "net.fetch",
            "ok": False,
            "duration_ms": 3,
            "error": "host 'x' is not in AGENT_NET_ALLOW_HOSTS",
            "preview": json.dumps({"error": "nope", "error_code": "net_denied"}),
        }
    )
    out = console.export_text()
    assert "failed" in out
    assert "not in AGENT_NET_ALLOW_HOSTS" in out
    assert "/net allow" in out  # the actionable hint
    r.handle({"type": "done", "steps": [{}], "duration_ms": 5, "usage": {}, "stop_reason": "stop"})
    assert "1 failed" in console.export_text()


def test_json_style_emits_one_object_per_event_and_skips_tokens(capsys):
    r, console = renderer("json")
    sample_turn(r)
    r.handle({"type": "token", "text": "hello"})
    # raw writes go straight to stdout so the format stays pipeable (no rich wrapping)
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    parsed = [json.loads(line) for line in lines]
    assert all(isinstance(item, dict) for item in parsed)
    assert [item["type"] for item in parsed] == ["tool_call", "tool_result", "done"]
    assert "hello" not in out
    assert len(max(lines, key=len)) == len(lines[1])  # no wrapping


def test_plain_style_keeps_the_historical_one_liners():
    r, console = renderer("plain")
    sample_turn(r)
    out = console.export_text()
    assert "→ exec.run" in out
    assert "ok 52ms" in out
    assert "─" not in out  # no gutter/tree decoration


def test_arguments_can_be_hidden():
    console = Console(record=True, width=120, force_terminal=False, no_color=True)
    r = make_renderer(console, "ide", 24, show_args=False)
    r.handle({"type": "tool_call", "index": 1, "tool": "exec.run", "arguments": {"argv": ["secret"]}})
    out = console.export_text()  # export_text() clears the buffer: capture once
    assert "secret" not in out
    assert "exec.run" in out


def test_agent_errors_are_loud():
    r, console = renderer()
    r.handle({"type": "error", "message": "LLM stream failed"})
    out = console.export_text()
    assert "agent error" in out
    assert "LLM stream failed" in out


@pytest.mark.parametrize("style", ["ide", "plain"])
def test_every_style_survives_a_full_turn(style: str):
    r, console = renderer(style)
    sample_turn(r)
    assert console.export_text().strip()


# ------------------------------------------------------------------------ folding


def test_long_output_is_folded_to_the_preview_and_summarised():
    r, console = renderer()
    r.handle({"type": "tool_call", "index": 1, "tool": "exec.run", "arguments": {"argv": ["ls", "-la", "/usr/bin"]}})
    r.handle(result_event(long_body(48), exit_code=0))
    out = console.export_text()
    # the header stays: tool, arguments, duration and the meta line
    assert "exec.run" in out
    assert "ls" in out and "/usr/bin" in out
    assert "10 ms" in out
    assert "exit_code=0" in out
    # only the first 6 lines of the output
    assert "line 0" in out and "line 5" in out
    assert "line 6" not in out
    assert "line 47" not in out
    # and one line saying exactly what was hidden
    assert "… 已折叠 42 行（共 48 行 · /more 看全文 · /log fold off 关闭折叠）" in out


def test_short_output_is_never_folded():
    r, console = renderer()
    r.handle({"type": "tool_call", "index": 1, "tool": "exec.run", "arguments": {}})
    r.handle(result_event("a\nb\nc"))
    out = console.export_text()
    assert "a" in out and "c" in out
    assert "已折叠" not in out


def test_output_at_the_preview_boundary_is_not_folded():
    r, console = renderer()
    r.handle({"type": "tool_call", "index": 1, "tool": "exec.run", "arguments": {}})
    r.handle(result_event(long_body(6)))
    out = console.export_text()
    assert "line 5" in out
    assert "已折叠" not in out


def test_folding_off_prints_the_whole_result():
    console = Console(record=True, width=200, force_terminal=False, no_color=True)
    r = make_renderer(console, "ide", 200, fold=False)
    r.handle({"type": "tool_call", "index": 1, "tool": "exec.run", "arguments": {}})
    r.handle(result_event(long_body(48)))
    out = console.export_text()
    assert "line 47" in out
    assert "已折叠" not in out


def test_very_long_output_that_does_not_fit_even_unfolded_keeps_the_old_hint():
    console = Console(record=True, width=200, force_terminal=False, no_color=True)
    r = make_renderer(console, "ide", 10, fold=False)
    r.handle({"type": "tool_call", "index": 1, "tool": "exec.run", "arguments": {}})
    r.handle(result_event(long_body(48)))
    out = console.export_text()
    assert "line 9" in out
    assert "line 10" not in out
    assert "+38 more line(s)" in out
    assert "/more" in out


def test_json_style_is_never_folded():
    r, console = renderer("json")
    r.handle({"type": "tool_call", "index": 1, "tool": "exec.run", "arguments": {}})
    r.handle(result_event(long_body(48)))
    out = console.export_text()
    assert "已折叠" not in out
    assert "fold" not in out


def test_folded_result_is_retained_for_more(capsys):
    r, console = renderer()
    r.handle({"type": "tool_call", "index": 1, "tool": "exec.run", "arguments": {}})
    r.handle(result_event(long_body(48)))
    console.export_text()
    assert r.retained == 1
    assert r.full_output(1).text.endswith("line 47")
    assert r.print_full_output(1) is True
    out = console.export_text()
    assert "完整输出" in out
    assert "line 47" in out
    assert "line 6" in out


def test_more_goes_back_through_results():
    r, console = renderer()
    for index in (1, 2):
        r.handle({"type": "tool_call", "index": index, "tool": "exec.run", "arguments": {}})
        r.handle({**result_event(long_body(20)), "index": index})
    console.export_text()
    assert r.retained == 2
    assert r.full_output(1).index == 2
    assert r.full_output(2).index == 1
    assert r.full_output(3) is None
    assert r.print_full_output(3) is False
    r.print_full_output(2)
    out = console.export_text()
    assert "第 2 近的结果" in out
    assert "line 19" in out


def test_retention_is_capped(monkeypatch):
    monkeypatch.setattr("agent.cli.render.HISTORY_MAX_RECORDS", 3)
    r, console = renderer()
    for index in range(1, 6):
        r.handle({"type": "tool_call", "index": index, "tool": "exec.run", "arguments": {}})
        r.handle({**result_event("ok"), "index": index})
    console.export_text()
    assert r.retained == 3
    assert r.full_output(1).index == 5
    assert r.full_output(3).index == 3
    assert r.full_output(4) is None


def test_total_text_budget_forces_the_oldest_result_out(monkeypatch):
    monkeypatch.setattr("agent.cli.render.HISTORY_MAX_CHARS", 150)
    r, console = renderer()
    for index in (1, 2, 3):
        r.handle({"type": "tool_call", "index": index, "tool": "exec.run", "arguments": {}})
        r.handle({**result_event("x" * 80), "index": index})
    console.export_text()
    assert r.retained == 1, "80+80 > 150, so only the newest (80) stays"
    assert r.full_output(1).index == 3
    assert r.full_output(2) is None


def test_total_text_budget_keeps_what_still_fits(monkeypatch):
    monkeypatch.setattr("agent.cli.render.HISTORY_MAX_CHARS", 200)
    r, console = renderer()
    for index in (1, 2, 3):
        r.handle({"type": "tool_call", "index": index, "tool": "exec.run", "arguments": {}})
        r.handle({**result_event("x" * 80), "index": index})
    console.export_text()
    assert r.retained == 2, "160 <= 200, but 240 is over the budget"
    assert (r.full_output(1).index, r.full_output(2).index) == (3, 2)


def test_failures_are_retained_too():
    r, console = renderer()
    r.handle({"type": "tool_call", "index": 1, "tool": "net.fetch", "arguments": {}})
    r.handle(
        {
            "type": "tool_result",
            "index": 1,
            "tool": "net.fetch",
            "ok": False,
            "duration_ms": 3,
            "error": "host 'x' is not in AGENT_NET_ALLOW_HOSTS",
            "preview": json.dumps({"error": "nope", "error_code": "net_denied"}),
        }
    )
    console.export_text()
    record = r.full_output(1)
    assert record is not None and record.ok is False
    assert "AGENT_NET_ALLOW_HOSTS" in record.text


# ------------------------------------------------------- answer block and spacing
# Every test here is a regression for a defect captured from a real run in the operator's
# terminal: padded lines, a lost gutter, collapsed code fences and 2-blank-line gaps.


def test_wrap_answer_keeps_the_line_boundary_it_was_given():
    """The streamed flush relies on this: a dropped newline glues whole lines together."""
    assert wrap_answer("```Linux\n", 40, after_prefix=False) == ANSWER_INDENT + "```Linux\n"
    assert wrap_answer("no newline", 40, after_prefix=False) == ANSWER_INDENT + "no newline"
    assert wrap_answer("\n", 40, after_prefix=False) == "\n", "a blank line stays genuinely blank"


def test_a_streamed_code_fence_stays_on_its_own_line():
    """Defect: `` ```Linux … ``` `` collapsed onto a single line."""
    r, buf = raw_renderer()
    answer = "内核是 6.1.0：\n\n```Linux\nLinux box 6.1\n```\n"
    stream_answer(r, answer)
    out = buf.getvalue()
    lines = [line.rstrip() for line in out.split("\n")]

    assert ANSWER_INDENT + "```Linux" in lines, "the fence opens on a line of its own"
    assert ANSWER_INDENT + "```" in lines, "and closes on a line of its own"
    assert ANSWER_INDENT + "Linux box 6.1" in lines, "the fenced body is untouched"
    assert "```LinuxLinux" not in out, "the old bug glued the fence to the next line"
    assert "\n" in out and out.count("\n") >= 5


def test_no_rendered_line_is_padded_to_the_terminal_width():
    """Defect: rich's Padding filled every line out to the terminal width with spaces."""
    r, buf = raw_renderer(width=60)
    r.handle({"type": "tool_call", "index": 1, "tool": "exec.run", "arguments": {}})
    r.handle(result_event("短行\n" + "x" * 90 + "\n", exit_code=0))
    r.handle({"type": "done", "steps": [{}], "duration_ms": 1, "usage": {}, "stop_reason": "stop"})
    out = buf.getvalue()

    padded = [line for line in out.split("\n") if line != line.rstrip()]
    assert padded == [], f"lines were padded with trailing spaces: {padded!r}"
    assert all(len(line) <= 60 for line in out.split("\n")), "and nothing overflows either"


def test_a_wrapped_tool_result_line_keeps_the_body_indent():
    """Defect: a continuation line lost the gutter the first line had."""
    r, buf = raw_renderer(width=48)
    r.handle({"type": "tool_call", "index": 1, "tool": "exec.run", "arguments": {}})
    r.handle(result_event("w" * 90, exit_code=0))
    body = [line for line in buf.getvalue().split("\n") if line.strip() and set(line.strip()) == {"w"}]

    assert len(body) >= 2, "the long line really did wrap"
    assert all(line.startswith(" " * (GUTTER_WIDTH + 2)) for line in body), body


def test_answer_continuation_lines_keep_the_left_gutter():
    """Defect: the answer's continuation lines drifted instead of lining up."""
    r, buf = raw_renderer(width=44)
    answer = (
        "第一段落写得很长，长到必须折行才能放进终端宽度里面去，所以它一定会占用不止一行。\n"
        "\n"
        "第二段。\n"
    )
    stream_answer(r, answer)
    lines = [line for line in buf.getvalue().split("\n") if line.strip()]

    assert lines[0].startswith(ANSWER_PREFIX + " "), lines[0]
    assert len(lines) >= 3, f"the first paragraph wrapped and the second is a new line: {lines!r}"
    for line in lines[1:]:
        assert line.startswith(ANSWER_INDENT), f"continuation lost its gutter: {line!r}"


def test_exactly_one_blank_line_between_the_blocks_of_a_turn():
    """Defect: two blank lines in one place (and none in another)."""
    r, buf = raw_renderer()
    r.handle({"type": "tool_call", "index": 1, "tool": "exec.run", "arguments": {}})
    r.handle(result_event("Linux sandbox-vm-1\n", exit_code=0))
    answer = "第一段。\n\n```Linux\nbox\n```\n"
    stream_answer(r, answer)
    r.handle({"type": "done", "steps": [{}], "duration_ms": 5, "usage": {}, "stop_reason": "stop"})
    out = buf.getvalue()

    assert set(blank_runs(out)) <= {1}, "no two blocks may be separated by two blank lines"
    assert "Linux sandbox-vm-1\n\n" + ANSWER_PREFIX in out, "the tool log and the answer are separated"
    assert "```\n\n1 tool call(s)" in out, "and so are the answer and the footer"


def test_two_turns_in_a_row_do_not_accumulate_blank_lines():
    r, buf = raw_renderer()
    for _ in range(2):
        r.handle({"type": "done", "steps": [{}], "duration_ms": 1, "usage": {}, "stop_reason": "stop"})
        r.finish_answer(final=True, text="回答\n")
    assert set(blank_runs(buf.getvalue())) <= {1}


def test_repeated_guest_noise_is_collapsed_in_the_preview_but_not_in_more():
    """The routine ``cgroup … Permission denied`` run is one line, /more keeps all of it."""
    r, buf = raw_renderer()
    noise = "limits: cgroup write /sys/fs/cgroup/agent/1/memory.max: Permission denied"
    body = "\n".join([noise] * 5 + ["the real output"])
    r.handle({"type": "tool_call", "index": 1, "tool": "exec.run", "arguments": {}})
    r.handle({**result_event(body), "exit_code": 0})
    out = buf.getvalue()

    assert out.count(noise) == 1, "five identical noise lines collapse to one"
    assert "重复了 4 次" in out
    assert "/more" in out
    assert "the real output" in out
    record = r.full_output(1)
    assert record is not None
    assert record.text.count(noise) == 5, "the retained text is untouched"


def test_collapse_repeats_leaves_short_runs_and_blank_lines_alone():
    assert collapse_repeats("a\na\nb") == "a\na\nb", "a run of 2 is not noise"
    assert collapse_repeats("a\na\na") == "a\n… 上一行重复了 2 次（共 3 行相同 · /more 看全文）"
    assert "重复" not in collapse_repeats("\n\n\n"), "blank lines are never collapsed"
    assert collapse_repeats("   \n   \n   \nx") == "   \n   \n   \nx", "nor is whitespace noise"

