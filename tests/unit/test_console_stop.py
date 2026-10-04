"""``/stop``: the console asks, runs the host-side teardown, then leaves the chat.

The teardown itself is *stubbed* here: these tests must never start or stop the real stack
(the operator is using it while this feature is being written).  They pin the exact command
line, what the operator sees, and -- the important half -- that the console stays alive when
the script is missing, cannot start, or fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

import agent.cli.console as console_module
from agent.cli.console import (
    COMMANDS,
    STOP_CONFIRM_PROMPT,
    STOP_MANUAL_HINT,
    STOP_SCRIPT,
    ConsoleState,
    SlashConsole,
    complete,
)

CAPTURED = "停止 AI 服务…"  # what the stub script "prints"


class _Process:
    """The bit of ``subprocess.Popen`` the command uses, plus its context manager."""

    def __init__(self, lines: tuple[str, ...], code: int) -> None:
        self.returncode = code
        self.stdout = iter([line.encode("utf-8") + b"\r\n" for line in lines])

    def __enter__(self) -> _Process:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class StubRunner:
    """Stands in for ``subprocess.Popen``: records argv, replays canned UTF-8 output."""

    def __init__(
        self,
        *,
        lines: tuple[str, ...] = (CAPTURED,),
        code: int = 0,
        error: OSError | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.lines = lines
        self.code = code
        self.error = error

    def __call__(self, argv, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        self.calls.append(list(argv))
        if self.error is not None:
            raise self.error
        return _Process(self.lines, self.code)

    @property
    def command(self) -> list[str]:
        assert self.calls, "the teardown was never started"
        return self.calls[-1]


def text_of(console: Console) -> str:
    return console.export_text()


def type_answer(monkeypatch, console: Console, answer: str, prompts: list[str]) -> None:
    """Stand in for the operator typing ``answer`` at the next confirmation prompt."""

    def fake_input(prompt: str = "", **kwargs) -> str:  # noqa: ANN003, ARG001
        prompts.append(prompt)
        return answer

    monkeypatch.setattr(console, "input", fake_input)


def never_ask(monkeypatch, console: Console) -> None:
    """Fail the test if the command asks anything (``/stop now`` / nothing to run)."""

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise AssertionError("this /stop must not ask for confirmation")

    monkeypatch.setattr(console, "input", boom)


@pytest.fixture
def shell(monkeypatch):
    """A console whose teardown is stubbed and whose /admin/config is off-limits."""
    console = Console(record=True, width=120, no_color=True, force_terminal=False)
    state = ConsoleState(session_id="s-1")
    sh = SlashConsole(console, state, console)

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise AssertionError("/stop is a host-side action: it must not call the AI service")

    monkeypatch.setattr(sh, "_admin", boom)
    return sh, console, state


def test_the_confirmation_prompt_is_exactly_the_documented_question():
    assert STOP_CONFIRM_PROMPT == "输入 y 确认关闭 agentbox（AI 服务 + 平台 VM + 沙箱 VM + 控制平面）；其它任意键取消："


def test_stop_asks_then_runs_the_teardown_and_exits(shell, monkeypatch):
    sh, console, state = shell
    runner = StubRunner(lines=(CAPTURED, "平台 VM 已关机"))
    monkeypatch.setattr(console_module.subprocess, "Popen", runner)
    prompts: list[str] = []
    type_answer(monkeypatch, console, "y", prompts)

    assert sh.handle("/stop") is True

    command = runner.command
    assert command[0] == "powershell"
    assert command[1] == "-NoProfile"
    assert " ".join(command[2:4]) == "-ExecutionPolicy Bypass"
    assert command[4] == "-File"
    assert command[5] == str(console_module.project_root() / "deploy" / "windows" / "stop-agent.ps1")
    assert "stop-agent.ps1" in " ".join(command)

    assert len(prompts) == 1, "the operator must be asked exactly once"
    assert STOP_CONFIRM_PROMPT in prompts[0]

    out = text_of(console)
    assert "即将关闭 agentbox" in out
    assert CAPTURED in out and "平台 VM 已关机" in out, "the script's output must be streamed"
    assert "已关闭" in out
    assert state.exit_requested is True, "a successful teardown must end the chat"


def test_stop_never_touches_the_service(shell, monkeypatch):
    """It must work with the AI service down: the fixture makes /admin/config fatal."""
    sh, console, state = shell
    runner = StubRunner()
    monkeypatch.setattr(console_module.subprocess, "Popen", runner)
    never_ask(monkeypatch, console)

    sh.handle("/stop now")

    assert len(runner.calls) == 1
    assert state.exit_requested is True


@pytest.mark.parametrize("answer", ["y", "Y", " y ", "y\r"])
def test_stop_confirms_on_y_whatever_the_case_or_padding(shell, monkeypatch, answer):
    sh, console, state = shell
    runner = StubRunner()
    monkeypatch.setattr(console_module.subprocess, "Popen", runner)
    type_answer(monkeypatch, console, answer, [])

    sh.handle("/stop")

    assert len(runner.calls) == 1
    assert state.exit_requested is True


@pytest.mark.parametrize("answer", ["", "n", "no", "yes", "是", "1"])
def test_stop_cancels_without_running_anything(shell, monkeypatch, answer):
    sh, console, state = shell
    runner = StubRunner()
    monkeypatch.setattr(console_module.subprocess, "Popen", runner)
    type_answer(monkeypatch, console, answer, [])

    assert sh.handle("/stop") is True

    assert runner.calls == [], "a cancelled /stop must not start the teardown"
    assert state.exit_requested is False, "a cancelled /stop must keep the console open"
    out = text_of(console)
    assert "已取消" in out
    assert "没有关闭任何东西" in out


def test_stop_now_skips_the_prompt_but_says_what_it_does(shell, monkeypatch):
    sh, console, state = shell
    runner = StubRunner()
    monkeypatch.setattr(console_module.subprocess, "Popen", runner)
    never_ask(monkeypatch, console)

    sh.handle("/stop now")

    assert len(runner.calls) == 1
    out = text_of(console)
    assert "即将关闭 agentbox" in out, "it must still announce the shutdown"
    assert "跳过确认" in out
    assert "运行：" in out and "stop-agent.ps1" in out, "the command line must be shown"
    assert state.exit_requested is True


def test_a_failing_script_reports_the_path_code_and_hint_without_exiting(shell, monkeypatch):
    sh, console, state = shell
    runner = StubRunner(lines=("停止失败：控制平面还在",), code=1)
    monkeypatch.setattr(console_module.subprocess, "Popen", runner)
    never_ask(monkeypatch, console)

    assert sh.handle("/stop now") is True

    out = text_of(console)
    assert str(console_module.project_root() / "deploy" / "windows" / "stop-agent.ps1") in out
    assert "退出码：1" in out
    assert STOP_MANUAL_HINT in out
    assert "控制台继续运行" in out
    assert state.exit_requested is False, "a failed teardown must leave the operator in control"


def test_a_missing_script_reports_the_exact_path_and_hint(shell, monkeypatch, tmp_path: Path):
    sh, console, state = shell
    runner = StubRunner()
    monkeypatch.setattr(console_module.subprocess, "Popen", runner)
    monkeypatch.setattr(console_module, "project_root", lambda: tmp_path)
    never_ask(monkeypatch, console)

    assert sh.handle("/stop") is True

    out = text_of(console)
    assert str(tmp_path / "deploy" / "windows" / "stop-agent.ps1") in out
    assert "退出码：（未运行）" in out
    assert STOP_MANUAL_HINT in out
    assert runner.calls == []
    assert state.exit_requested is False


def test_a_powershell_that_cannot_start_does_not_exit_the_console(shell, monkeypatch):
    sh, console, state = shell
    runner = StubRunner(error=FileNotFoundError("powershell"))
    monkeypatch.setattr(console_module.subprocess, "Popen", runner)
    never_ask(monkeypatch, console)

    assert sh.handle("/stop now") is True

    out = text_of(console)
    assert "无法启动关闭脚本" in out
    assert STOP_MANUAL_HINT in out
    assert state.exit_requested is False


def test_stop_only_accepts_now_as_an_argument(shell, monkeypatch):
    sh, console, state = shell
    runner = StubRunner()
    monkeypatch.setattr(console_module.subprocess, "Popen", runner)

    sh.handle("/stop --force")

    assert runner.calls == []
    assert state.exit_requested is False
    out = text_of(console)
    assert "用法：/stop" in out
    assert "/stop now" in out, "the refusal must show the one accepted argument"


# --------------------------------------------------------- help and completion


def test_stop_is_discoverable_by_completion():
    assert "stop" in COMMANDS
    assert complete("/st") == ["/stop"]
    assert complete("/stop ") == ["/stop now"]
    assert complete("/stop n") == ["/stop now"]
    assert complete("/stop now ") == []
    assert "/stop" in complete("/s")
    assert complete("/") == [f"/{name}" for name in COMMANDS]


def test_help_stop_documents_both_forms(shell):
    sh, console, _ = shell
    sh.handle("/help stop")
    out = text_of(console)
    assert "/stop" in out and "/stop now" in out
    assert "stop-agent.ps1" in out
    assert "-ExecutionPolicy Bypass" in out
    assert "/admin/config" in out, "the help must say it does not depend on the AI service"
    assert "确认" in out
    assert "/exit" in out, "the help must point at the command that only leaves the chat"


def test_help_overview_lists_stop(shell):
    sh, console, _ = shell
    sh.handle("/help")
    out = text_of(console)
    assert "/stop" in out
    assert "/help stop" in out


def test_the_teardown_path_is_the_deploy_script():
    assert str(STOP_SCRIPT).replace("\\", "/") == "deploy/windows/stop-agent.ps1"
    assert (console_module.project_root() / STOP_SCRIPT).is_file(), "the script ship must exist"
