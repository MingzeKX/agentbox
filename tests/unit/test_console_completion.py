"""Console completion, per-command help, and cancelling a persona.

These exist because the operator could not discover the commands: `/help` was one long
blob, there was no tab completion, and nothing said how to stop role-play.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from agent.ai import admin
from agent.cli.console import COMMANDS, ConsoleState, SlashConsole, complete


def test_bare_slash_lists_every_command():
    candidates = complete("/")
    assert len(candidates) == len(COMMANDS)
    assert "/persona" in candidates and "/net" in candidates


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/pe", {"/perm", "/persona"}),
        ("/per", {"/perm", "/persona"}),
        ("/n", {"/net", "/new"}),
        ("/s", {"/save", "/sandbox", "/session", "/speak", "/stop"}),
    ],
)
def test_prefix_completion(text, expected):
    assert set(complete(text)) == expected


def test_voice_mode_completion_offers_every_mode():
    """The new voice commands must be discoverable, and only the first argument completes."""
    assert set(complete("/voice-mode ")) == {
        "/voice-mode on",
        "/voice-mode off",
        "/voice-mode status",
        "/voice-mode hands-free",
    }
    assert complete("/voice-mode h") == ["/voice-mode hands-free"]
    assert complete("/voice-mode hands-free ") == []
    assert complete("/speak ") == ["/speak on", "/speak off"]
    assert "/voice-devices" in complete("/voice-d")
    assert complete("/voice-devices ") == [], "there is nothing to type after it"


def test_subcommand_completion():
    assert set(complete("/perm ")) == {"/perm safe", "/perm trusted", "/perm unrestricted"}
    assert "/net allow" in complete("/net ")
    assert set(complete("/log ")) >= {"/log ide", "/log plain", "/log json"}


def test_log_completion_offers_fold():
    assert "/log fold" in complete("/log ")
    assert complete("/log f") == ["/log fold"]


def test_new_commands_are_discoverable():
    assert "/more" in complete("/mo")
    assert "/voice" in complete("/vo")
    assert complete("/") == [f"/{name}" for name in COMMANDS]


def test_voice_completes_the_command_but_not_paths():
    assert "/voice" in complete("/v")
    assert complete("/voice ") == [], "audio paths are typed, not completed"
    assert complete("/voice C:/tmp/note.wav ") == []


def test_model_and_think_completion_from_the_known_lists():
    assert complete("/model ", models=["deepseek-flash", "deepseek-v4-pro"]) == [
        "/model deepseek-flash",
        "/model deepseek-v4-pro",
    ]
    assert complete("/model deepseek-f", models=["deepseek-flash", "deepseek-v4-pro"]) == [
        "/model deepseek-flash"
    ]
    assert complete("/model deepseek-v", models=["deepseek-flash", "deepseek-v4-pro"]) == ["/model deepseek-v4-pro"]
    assert complete("/model nomatch", models=["deepseek-flash", "deepseek-v4-pro"]) == []
    assert complete("/think ") == ["/think low", "/think high", "/think max", "/think default"]
    assert complete("/think m") == ["/think max"]


def test_model_completion_falls_back_to_the_known_ids(monkeypatch):
    """With no service (and no injected list) the picker must still offer the two known ids."""
    import agent.cli.console as console_module

    monkeypatch.setattr(console_module, "known_models", lambda: ["deepseek-flash", "deepseek-v4-pro"])
    assert complete("/model ") == ["/model deepseek-flash", "/model deepseek-v4-pro"]


def test_known_models_survives_an_unreachable_service(monkeypatch):
    import httpx

    from agent.cli.console import known_models

    monkeypatch.setattr("httpx.get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    assert known_models() == ["deepseek-flash", "deepseek-v4-pro"]


def test_new_image_commands_are_discoverable():
    assert "/image" in complete("/im")
    assert "/image-clear" in complete("/image-")
    assert complete("/image ") == [], "image paths are typed, not completed"
    assert complete("/image C:/tmp/a.png 什么颜色 ") == []


def test_new_commands_are_in_the_overview():
    candidates = complete("/")
    for name in ("/model", "/think", "/image", "/image-clear", "/voice-mode", "/voice-devices", "/speak"):
        assert name in candidates


def test_persona_completion_includes_how_to_cancel():
    listed = complete("/persona ")
    assert "/persona roleplay" in listed
    assert "/persona off" in listed, "cancelling role-play must be discoverable"
    assert complete("/persona o") == ["/persona off"]


def test_persona_completion_can_use_the_service_list():
    listed = complete("/persona ", personas=["engineer", "roleplay"])
    assert listed == ["/persona engineer", "/persona roleplay"]


def test_plain_text_and_free_arguments_are_not_completed():
    assert complete("hello there") == []
    assert complete("/net allow example.com ") == []


# --------------------------------------------------------------------------- help


@pytest.fixture(autouse=True)
def _never_ssh(monkeypatch):
    """The automatic .env write is checked in test_console_commands; here it must not run.

    Without this, a mutating command in this file would really try to ssh into the VM.
    """
    monkeypatch.setattr(
        SlashConsole,
        "_persist",
        lambda self, keys: (_ for _ in ()).throw(AssertionError(f"unexpected .env write: {keys}")),
    )


@pytest.fixture
def shell():
    console = Console(record=True, width=110, no_color=True, force_terminal=False)
    state = ConsoleState(log_style="ide", log_lines=24)
    return SlashConsole(console, state, console), console


def text_of(console: Console) -> str:
    return console.export_text()


def test_help_overview_lists_every_command(shell):
    sh, console = shell
    sh.handle("/help")
    out = text_of(console)
    for command in ("/net", "/perm", "/persona", "/log", "/save", "/sandbox", "/clear"):
        assert command in out
    for command in ("/more", "/voice"):
        assert command in out, f"{command} must be discoverable from /help"
    assert "按 Tab 补全" in out


def test_help_log_documents_folding(shell):
    sh, console = shell
    sh.handle("/help log")
    out = text_of(console)
    assert "/log fold on|off" in out
    assert "折叠" in out
    assert "/more" in out


def test_help_more_documents_the_retention_cap(shell):
    sh, console = shell
    sh.handle("/help more")
    out = text_of(console)
    assert "/more 2" in out
    assert "完整" in out
    assert "5 个" in out and "200 KB" in out


def test_help_voice_documents_the_command(shell):
    sh, console = shell
    sh.handle("/help voice")
    out = text_of(console)
    assert "/voice <音频文件>" in out
    assert "/asr" in out
    assert "识别到" in out


def test_help_image_documents_formats_limits_and_the_default_question(shell):
    sh, console = shell
    sh.handle("/help image")
    out = text_of(console)
    assert "/image <图片路径>" in out
    assert "png" in out and "jpeg" in out and "webp" in out and "gif" in out
    assert "AGENT_LLM_MAX_IMAGES" in out and "AGENT_LLM_MAX_IMAGE_BYTES" in out
    assert "请描述这张图里有什么" in out
    assert "/image-clear" in out
    assert "视觉模型" in out and "deepseek-flash" in out


def test_help_model_lists_the_two_known_ids(shell):
    sh, console = shell
    sh.handle("/help model")
    out = text_of(console)
    assert "/model deepseek-flash" in out and "/model deepseek-v4-pro" in out
    assert "GET /llm" in out
    assert "能看图" in out


def test_help_think_is_honest_about_the_noisy_effect(shell):
    sh, console = shell
    sh.handle("/help think")
    out = text_of(console)
    assert "/think low|high|max" in out
    assert "default" in out and "不发送" in out
    assert "不稳定" in out, "the help must say the effect is unreliable"
    assert "104" in out and "156" in out and "124" in out, "the measured medians must be stated"
    assert "网关也照收" in out
    for overstated in ("显著变好", "明显提升", "效果显著"):
        assert overstated not in out


def test_overview_mentions_the_new_commands(shell):
    sh, console = shell
    sh.handle("/help")
    out = text_of(console)
    for command in ("/model", "/think", "/image"):
        assert command in out, f"{command} must be discoverable from /help"


def test_help_for_one_command_explains_it(shell):
    sh, console = shell
    sh.handle("/help persona")
    out = text_of(console)
    assert "/persona off" in out
    assert "取消角色扮演" in out
    assert "不能更改权限" in out


def test_help_net_documents_the_firewall(shell):
    sh, console = shell
    sh.handle("/help net")
    out = text_of(console)
    assert "/net allow" in out and "/net ports" in out and "/net private" in out
    assert "白名单" in out and "端口" in out


def test_help_unknown_topic_points_at_the_known_ones(shell):
    sh, console = shell
    sh.handle("/help frobnicate")
    out = text_of(console)
    assert "没有 /frobnicate 的帮助" in out
    assert "已知命令" in out, "the refusal must still list the known commands"


# ------------------------------------------------------------- cancelling a persona


class FakeConsole(SlashConsole):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[dict] = []
        self.persisted: list[dict] = []
        self.values = {"persona": "roleplay", "permission_tier": "safe"}

    def _admin(self, changes=None):  # noqa: ANN001
        if changes is not None:
            self.calls.append(dict(changes))
            self.values.update(changes)
        return {"ok": True, "effective": dict(self.values), "applied": [f"{k}={v}" for k, v in (changes or {}).items()]}

    def _persist(self, keys):  # noqa: ANN001
        self.persisted.append(dict(keys))


@pytest.mark.parametrize("word", ["off", "none", "default", "neutral"])
def test_persona_can_be_cancelled_with_a_synonym(word):
    console = Console(record=True, width=110, no_color=True, force_terminal=False)
    sh = FakeConsole(console, ConsoleState(), console)
    sh.handle(f"/persona {word}")
    assert sh.calls == [{"persona": "engineer"}], "cancelling must fall back to the neutral persona"
    assert "已取消角色扮演" in text_of(console)


def test_admin_maps_the_alias_too():
    effective, _ = admin.apply({"persona": "off"})
    assert effective["persona"] == "engineer"
    admin.apply({"persona": "engineer"})


# ------------------------------------------------------------------------- /clear


class ClearRecordingConsole(Console):
    """A rich Console that remembers whether clear() was called."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cleared = 0

    def clear(self, home: bool = True) -> None:  # noqa: FBT001, FBT002 - rich's own signature
        self.cleared += 1
        super().clear(home)


def test_clear_is_completable():
    candidates = complete("/cl")
    assert "/clear" in candidates and "/cls" in candidates


def test_clear_clears_the_screen_and_keeps_the_session():
    console = ClearRecordingConsole(record=True, width=110, no_color=True, force_terminal=False)
    state = ConsoleState(session_id="s-1")
    sh = SlashConsole(console, state, console)
    assert sh.handle("/clear") is True
    assert console.cleared == 1, "/clear must clear the terminal"
    assert "已清屏" in text_of(console)
    assert state.session_id == "s-1", "/clear must not touch the session"
    assert state.new_session is False


def test_cls_is_an_alias_for_clear():
    console = Console(record=True, width=110, no_color=True, force_terminal=False)
    sh = SlashConsole(console, ConsoleState(), console)
    assert sh.handle("/cls") is True
    assert "已清屏" in text_of(console)


def test_clear_works_without_the_service(monkeypatch):
    """Local command: it must not reach /admin/config, so it works while the service is down."""
    console = Console(record=True, width=110, no_color=True, force_terminal=False)
    sh = SlashConsole(console, ConsoleState(), console)

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise AssertionError("/clear must not talk to the service")

    monkeypatch.setattr(sh, "_admin", boom)
    assert sh.handle("/clear") is True
    assert "已清屏" in text_of(console)


# ------------------------------------------- main.py: what /image puts on the wire


class _FakeStream:
    """Stands in for httpx.stream(), capturing the request body."""

    def __init__(self, seen: dict, events: list[str], method: str = "", url: str = "", **kwargs) -> None:
        self.seen = seen
        self.events = events
        self.status_code = 200
        seen["method"] = method
        seen["url"] = url
        seen["json"] = kwargs.get("json")

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def iter_lines(self):
        yield from self.events


def test_image_command_puts_the_images_on_the_wire(shell, tmp_path, monkeypatch):
    import argparse
    import base64

    import httpx

    from agent.cli.main import _stream_turn

    png = b"\x89PNG\r\n\x1a\n" + b"pixels" * 4
    path = tmp_path / "shot.png"
    path.write_bytes(png)

    sh, console = shell
    state = sh.state
    sh.handle(f"/image {path} 这是什么颜色？")
    assert state.pending_images and state.pending_message == "这是什么颜色？"

    seen: dict = {}
    events = [
        'data: {"type": "session", "session_id": "s-live"}',
        'data: {"type": "done", "steps": [], "duration_ms": 1, "usage": {}, "stop_reason": "stop"}',
        "data: [DONE]",
    ]
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStream(seen, events, *a, **k))

    args = argparse.Namespace(log_style="plain", log_lines=24, no_args=False, log_fold=True)
    resolved = _stream_turn(state.pending_message or "", "s-1", args, {}, images=state.pending_images)
    assert resolved == "s-live"

    payload = seen["json"]
    assert payload["message"] == "这是什么颜色？"
    assert payload["stream"] is True
    assert len(payload["images"]) == 1
    sent = payload["images"][0]
    assert sent["media_type"] == "image/png"
    assert base64.b64decode(sent["data_base64"]) == png


def test_plain_turns_carry_no_images_key(monkeypatch):
    import argparse
    import json

    import httpx

    from agent.cli.main import _stream_turn

    seen: dict = {}
    events = ['data: {"type": "done", "steps": [], "duration_ms": 1, "usage": {}, "stop_reason": "stop"}']
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStream(seen, events, *a, **k))

    args = argparse.Namespace(log_style="plain", log_lines=24, no_args=False, log_fold=True)
    _stream_turn("你好", "s-1", args, {})
    assert "images" not in seen["json"], "a text-only turn must stay byte-for-byte as before"
    assert json.loads(json.dumps(seen["json"]))["message"] == "你好"


# ------------------------------------------------- main.py: the interactive chrome

#: command names, flag names, rich markup, paths and internal keys stay as they are
_ASCII_FRAGMENTS = (
    "agentbox",
    "/help",
    "/net",
    "/persona",
    "/exit",
    "/log",
    "/more",
    "/voice",
    "/asr",
    "/admin/config",
    "ide",
    "plain",
    "json",
    "log_style",
    "log_lines",
    "log_fold",
    "renderer",  # the stream_state key, not text
    "cancelled",  # the stream_state key set by Ctrl-C, not text
    "c-t",  # the prompt_toolkit binding for the talk key
    "f2",  # its function-key alias
    "chat-history",  # the history file name
    "you › ",  # the input prompt, same word /clear etc. keep
    "cyan",  # a panel border style
)


def _chat_source() -> tuple[str, list[str]]:
    """The source of ``cmd_chat`` and every string literal it contains."""
    import ast
    from pathlib import Path

    import agent.cli.main as cli_main

    source = Path(cli_main.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "cmd_chat"
    )
    literals = [node.value for node in ast.walk(function) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    return source, literals


def test_interactive_session_banner_is_chinese():
    source, _ = _chat_source()
    for phrase in ("交互会话", "/help 查看命令", "取消角色扮演", "/exit 退出"):
        assert phrase in source, f"the session banner must stay Chinese: {phrase!r} missing"
    for gone in ("interactive session", "for commands", "to quit", "tab completes", "completion unavailable"):
        assert gone not in source, f"untranslated chrome is back: {gone!r}"


def test_cmd_chat_string_literals_are_chinese():
    """No English prose may reach the interactive session (markup and keys excepted)."""
    _, literals = _chat_source()
    offenders = [
        text
        for text in literals
        if any(char.isalpha() and char.isascii() for char in text)
        and not any("\u4e00" <= char <= "\u9fff" for char in text)
        and "[" not in text  # rich markup such as [bold cyan]
        and not any(fragment in text for fragment in _ASCII_FRAGMENTS)
    ]
    assert offenders == [], f"user-facing text in cmd_chat must be Chinese: {offenders}"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], True),
        (["--log-fold"], True),
        (["--no-log-fold"], False),
    ],
)
def test_log_fold_flag_defaults_to_on(argv, expected):
    from agent.cli.main import build_parser

    args = build_parser().parse_args(["chat", *argv])
    assert args.log_fold is expected


def test_log_fold_flags_are_mutually_exclusive():
    from agent.cli.main import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["chat", "--log-fold", "--no-log-fold"])
