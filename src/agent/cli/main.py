"""Rich command line interface for operator tasks and interactive chat."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent.cli.render import make_renderer
from agent.cli.voice import (
    MODE_HANDS_FREE,
    MODE_OFF,
    MODE_PUSH_TO_TALK,
    TALK_KEY,
    TALK_KEY_ALT,
    TALK_SENTINEL,
    TalkKey,
)
from agent.config import project_root, settings
from agent.logging_setup import setup_logging

console = Console()
err_console = Console(stderr=True, style="bold red")

#: stream_state key holding the assistant text of the current turn (voice mode speaks it)
ANSWER_TEXT_KEY = "answer_text"

#: stream_state key holding the console's inline-image hook: text about to be printed in,
#: how many sandbox pictures were painted out (the renderer calls it, see render.StepRenderer)
IMAGE_HOOK_KEY = "image_hook"

#: printed when the operator cancelled a turn with Ctrl-C (the console stays alive)
TURN_CANCELLED = "[dim]已取消这一轮（Ctrl-C）——回到提示符，可以继续输入或再按回车说话[/dim]"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _table(title: str, columns: list[str]) -> Table:
    table = Table(title=title, header_style="bold cyan", show_lines=False)
    for column in columns:
        table.add_column(column)
    return table


def _ok(value: bool) -> Text:
    return Text("ok", style="bold green") if value else Text("FAIL", style="bold red")


def _control_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.control_secret:
        headers["X-Agent-Token"] = settings.control_secret
    return headers


def _control_candidates() -> list[str]:
    """Where the control plane might be, in preference order.

    The host CLI must use loopback; a CLI running *inside* the platform VM must use
    10.0.2.2 (slirp's view of the host).  Rather than making the user pick, probe.
    """
    urls = [settings.control_url_local.rstrip("/"), settings.control_url.rstrip("/")]
    return list(dict.fromkeys(urls))


def _control_base(timeout: float = 2.0) -> str:
    for url in _control_candidates():
        try:
            if httpx.get(f"{url}/health", timeout=timeout).status_code == 200:
                return url
        except httpx.HTTPError:
            continue
    return _control_candidates()[0]


def _control_rpc(method: str, params: dict[str, Any] | None = None, timeout: float = 120.0) -> dict[str, Any]:
    url = f"{_control_base()}/rpc"
    try:
        response = httpx.post(
            url, json={"method": method, "params": params or {}}, headers=_control_headers(), timeout=timeout
        )
    except httpx.HTTPError as exc:
        raise SystemExit(
            f"control plane unreachable at {url}: {exc}\n"
            f"checked: {', '.join(_control_candidates())}\n"
            "on the Windows host start it with: "
            r".\.venv\Scripts\python -m agent.cli serve control"
        ) from exc
    body = response.json()
    if not body.get("ok"):
        error = body.get("error") or {}
        raise SystemExit(f"{method} failed: [{error.get('code')}] {error.get('message')}")
    return body.get("result") or {}


def _ai_rpc(path: str, method: str = "GET", payload: dict[str, Any] | None = None, timeout: float = 300.0) -> Any:
    url = f"http://127.0.0.1:{settings.ai_port}{path}"
    try:
        response = httpx.request(method, url, json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        raise SystemExit(f"AI service unreachable at {url}: {exc}") from exc
    if response.status_code >= 400:
        raise SystemExit(f"AI service returned HTTP {response.status_code}: {response.text[:500]}")
    return response.json()


# --------------------------------------------------------------------------- #
# serve
# --------------------------------------------------------------------------- #


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    if args.role == "ai":
        target, host, port = "agent.ai.app:app", args.host or settings.ai_host, args.port or settings.ai_port
    else:
        target, host, port = "agent.control.app:app", args.host or settings.control_host, args.port or settings.control_port
    console.print(f"[bold]starting {args.role} service[/bold] on http://{host}:{port}")
    uvicorn.run(target, host=host, port=port, log_level=settings.log_level.lower(), reload=bool(args.reload))
    return 0


# --------------------------------------------------------------------------- #
# chat
# --------------------------------------------------------------------------- #


def _render_event(event: dict[str, Any], stream_state: dict[str, Any]) -> None:
    """Delegate to the configured renderer (ide / plain / json).

    The renderer is created once per turn but ``/more`` needs it to survive between
    turns, so a caller may pass one in (``stream_state["renderer"]``); its display
    settings are re-read from the stream state every event.

    The assistant's own text is also accumulated into ``stream_state[ANSWER_TEXT_KEY]``:
    the renderer prints it, and voice mode needs the same words to speak them.
    """
    if event.get("type") == "token":
        stream_state[ANSWER_TEXT_KEY] = str(stream_state.get(ANSWER_TEXT_KEY) or "") + str(
            event.get("text") or ""
        )
    renderer = stream_state.get("renderer")
    if renderer is None:  # a caller passed a bare state dict
        renderer = make_renderer(
            console,
            stream_state.get("log_style", "plain"),
            stream_state.get("log_lines", 24),
            show_args=stream_state.get("show_args", True),
            fold=stream_state.get("fold", True),
        )
        stream_state["renderer"] = renderer
    renderer.style = stream_state.get("log_style", renderer.style)
    renderer.max_lines = stream_state.get("log_lines", renderer.max_lines)
    renderer.show_args = stream_state.get("show_args", renderer.show_args)
    renderer.fold = stream_state.get("fold", renderer.fold)
    # the console's inline-image hook: text about to be printed is scanned for sandbox
    # pictures, which are then pulled and painted right there (None = feature off)
    renderer.image_hook = stream_state.get(IMAGE_HOOK_KEY)
    renderer.handle(event)


POST_QUEUE_DONE = object()
"""Queue sentinel: the reader thread has finished (or failed)."""


def _chat_worker(payload: dict[str, Any], url: str, events: queue.Queue, stop: threading.Event) -> None:
    """Read the chat stream into ``events`` so Ctrl-C can abandon it from the main thread.

    The reader lives on a worker because ``iter_lines()`` only notices Ctrl-C between
    lines: a turn that is thinking (or waiting on the first token) would otherwise keep the
    operator waiting.  ``stop`` makes the thread abandon its socket as soon as the main
    thread gives up on the turn.
    """
    try:
        with httpx.stream("POST", url, json=payload, timeout=1800.0) as response:
            if response.status_code >= 400:
                events.put({"type": "_http_error", "status": response.status_code})
                return
            for line in response.iter_lines():
                if stop.is_set():
                    return
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                events.put(event)
    except httpx.HTTPError as exc:
        if not stop.is_set():
            events.put({"type": "_transport_error", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 - a broken stream must not kill the reader thread
        if not stop.is_set():
            events.put({"type": "_transport_error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        events.put(POST_QUEUE_DONE)


def _stream_turn(
    message: str,
    session_id: str | None,
    args: argparse.Namespace | None = None,
    stream_state: dict[str, Any] | None = None,
    images: list[dict[str, Any]] | None = None,
) -> str | None:
    style = getattr(args, "log_style", "ide")
    log_lines = getattr(args, "log_lines", 24)
    payload: dict[str, Any] = {
        "message": message,
        "session_id": session_id,
        "stream": True,
        # the IDE view shows whole results, so ask for more than the 400 char default
        "preview_chars": 16_000 if style == "ide" else 400,
    }
    if images:
        # /image: inline base64 parts.  The AI service switches this turn to its vision
        # model by itself, so nothing model related has to be decided here.
        payload["images"] = list(images)
    url = f"http://127.0.0.1:{settings.ai_port}/chat"
    if stream_state is None:
        stream_state = {}
    stream_state.setdefault("renderer", None)
    stream_state.update(
        {
            "streaming": False,
            # the answer of this turn only (voice mode speaks it back)
            ANSWER_TEXT_KEY: "",
            "log_style": style,
            "log_lines": log_lines,
            "show_args": not getattr(args, "no_args", False),
            "fold": getattr(args, "log_fold", True),
            "cancelled": False,
        }
    )
    resolved = session_id
    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    reader = threading.Thread(
        target=_chat_worker, args=(payload, url, events, stop), name="agentbox-chat", daemon=True
    )
    reader.start()
    try:
        while True:
            try:
                event = events.get(timeout=0.2)
            except queue.Empty:
                continue
            if event is POST_QUEUE_DONE:
                break
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "_http_error":
                err_console.print(f"AI service returned HTTP {event.get('status')}")
                break
            if kind == "_transport_error":
                err_console.print(f"chat failed: {event.get('message')}")
                break
            if kind == "session":
                resolved = event.get("session_id") or resolved
                continue
            _render_event(event, stream_state)
    except KeyboardInterrupt:
        # "Ctrl-C must cancel that turn and return to the prompt without killing the
        # console": drop the stream, say what happened, and let the caller ask again.
        stop.set()
        stream_state["cancelled"] = True
        console.print()
        console.print(TURN_CANCELLED)
    finally:
        renderer = stream_state.get("renderer")
        answer = str(stream_state.get(ANSWER_TEXT_KEY) or "")
        if renderer is not None:
            # close the tool block and print the whole answer once, wrapped and prefixed
            renderer.finish_answer(final=True, text=answer)
        if stream_state.get("streaming"):
            console.file.write("\n")
    return resolved



def cmd_chat(args: argparse.Namespace) -> int:
    session_id = args.session
    if args.message:
        _stream_turn(args.message, session_id, args)
        return 0

    from agent.cli.console import ConsoleState, SlashConsole, complete

    if args.no_speak:
        # --no-speak: keep voice mode silent for this session (AGENT_VOICE_TTS is the default)
        settings.voice_tts = False

    state = ConsoleState(
        session_id=session_id,
        log_style=args.log_style,
        log_lines=args.log_lines,
        show_args=not args.no_args,
        fold=args.log_fold,
        voice_mode=MODE_PUSH_TO_TALK if args.voice else MODE_OFF,
    )
    slash = SlashConsole(console, state, err_console)
    # one renderer for the whole session: /more needs what earlier turns rendered.
    # image_hook is how a reply that names a sandbox picture also shows it in place.
    stream_state: dict[str, Any] = {IMAGE_HOOK_KEY: slash.show_inline_images}

    # Tab completion + history, when prompt_toolkit is available (it is a normal
    # dependency, but the console must still work if the import fails).  The talk key is
    # bound here as well as in the fallback prompt below: pressing it *while a line is
    # being typed* starts a recording instead of inserting a character.
    read_line = None
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings

        class SlashCompleter(Completer):
            def get_completions(self, document, complete_event):  # noqa: ANN001, ARG002
                for candidate in complete(document.text_before_cursor):
                    yield Completion(candidate, start_position=-len(document.text_before_cursor))

        bindings = KeyBindings()

        def _talk(event) -> None:  # noqa: ANN001 - prompt_toolkit key handler
            # hand the sentinel to the caller: a typed space must never mean "talk"
            event.app.exit(result=TALK_SENTINEL)

        bindings.add("c-t")(_talk)
        bindings.add("f2")(_talk)
        history = settings.var_dir / "chat-history"
        history.parent.mkdir(parents=True, exist_ok=True)
        session = PromptSession(
            history=FileHistory(str(history)), completer=SlashCompleter(), key_bindings=bindings
        )
        read_line = lambda prompt: session.prompt(prompt)  # noqa: E731
        console.print(f"[dim]Tab 补全控制台命令 · {TALK_KEY}/{TALK_KEY_ALT} 说话 · /help 查看命令列表[/dim]")
    except Exception as exc:  # noqa: BLE001 - no completion, still usable
        console.print(f"[dim]补全不可用（{type(exc).__name__}）；/help 仍然可用[/dim]")

    def ask(prompt: str) -> Any:
        # one line from the operator; falls back when there is no TTY (piped input).
        # The talk key returns TALK_SENTINEL -- never the empty string, and never a space.
        nonlocal read_line
        if read_line is not None:
            try:
                return read_line(prompt)
            except (EOFError, KeyboardInterrupt):
                raise
            except Exception:  # noqa: BLE001 - no TTY (piped input, dumb terminal)
                read_line = None
        return console.input(f"[bold cyan]{prompt}[/bold cyan] ")

    def turn(message: str, images: list[dict[str, Any]] | None = None) -> str | None:
        # send one message and return the answer text (voice mode speaks it)
        nonlocal session_id
        console.print()
        session_id = _stream_turn(message, session_id, args, stream_state, images=images)
        state.session_id = session_id
        state.renderer = stream_state.get("renderer")
        console.print()
        if stream_state.get("cancelled"):
            return None
        return stream_state.get(ANSWER_TEXT_KEY) or None

    console.print(
        Panel(
            "[bold]agentbox[/bold] 交互会话\n"
            "[dim]/help 查看命令 · /net on 联网 · /persona roleplay 角色扮演 · "
            "/persona off 取消角色扮演 · /voice-mode on 语音模式 · /exit 退出[/dim]",
            border_style="cyan",
        )
    )
    if state.voice_mode != MODE_OFF:
        console.print(f"[dim]已进入语音模式（--voice）：按回车或 {TALK_KEY} 说话，q 退出语音模式。[/dim]")
    while True:
        if state.voice_mode != MODE_OFF:
            from agent.cli.voice import voice_loop

            args.log_style = state.log_style
            args.log_lines = state.log_lines
            args.no_args = not state.show_args
            args.log_fold = state.fold
            state.voice_mode = voice_loop(
                console=console,
                ask=ask,
                answer=turn,
                hands_free=state.voice_mode == MODE_HANDS_FREE,
            )
            continue
        try:
            entered = ask("you › ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        # a lone space (or any other text) is a message; only the talk key is special, and
        # it is only meaningful inside voice mode
        line = "" if isinstance(entered, TalkKey) else str(entered).strip()
        if not line:
            continue
        if slash.handle(line):
            if state.exit_requested:
                break
            if state.new_session:
                session_id = None
            # keep the renderer in sync with /log changes
            args.log_style = state.log_style
            args.log_lines = state.log_lines
            args.no_args = not state.show_args
            args.log_fold = state.fold
            if state.renderer is not None:
                stream_state["renderer"] = state.renderer
            if state.pending_message or state.pending_images:
                # /voice queues a transcript, /image queues pictures: send them now so the
                # operator does not have to retype anything
                message, state.pending_message = state.pending_message, None
                images, state.pending_images = state.pending_images, []
                turn(message or "", images)
            continue
        turn(line)
    return 0


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #


def _admin_headers() -> dict[str, str]:
    """Headers for the AI service's ``/admin/*`` surface: the control token, when set."""
    headers = {"Content-Type": "application/json"}
    if settings.control_secret:
        headers["X-Agent-Token"] = settings.control_secret
    return headers


def _admin_rpc(
    path: str, payload: dict[str, Any] | None = None, *, method: str = "GET", timeout: float = 60.0
) -> dict[str, Any]:
    """Call one ``/admin/*`` endpoint, or SystemExit with the service's own message.

    The sibling work on the registry adds ``GET /admin/tools``, ``POST /admin/tools/retire``
    and ``POST /admin/tools/delete``; this is the CLI half of that contract, so a 401 (wrong
    or missing token) and a refusal both read as one Chinese-aware line instead of a
    traceback.
    """
    url = f"http://127.0.0.1:{settings.ai_port}{path}"
    try:
        response = httpx.request(method, url, json=payload, headers=_admin_headers(), timeout=timeout)
    except httpx.HTTPError as exc:
        raise SystemExit(f"无法连接 AI 服务：{exc}") from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise SystemExit(f"AI 服务返回了无法解析的响应（HTTP {response.status_code}）") from exc
    if response.status_code == 401:
        raise SystemExit("被拒绝（HTTP 401）：控制面令牌不匹配，检查 AGENT_CONTROL_SECRET")
    if response.status_code >= 400 or not body.get("ok"):
        raise SystemExit(body.get("error") or f"HTTP {response.status_code}")
    return body


def _tool_version(name: str, version: int | None, *, timeout: float = 60.0) -> int:
    """The version to act on: the one asked for, else the single active one."""
    if version is not None:
        return int(version)
    body = _admin_rpc("/admin/tools", timeout=timeout)
    rows = [row for row in (body.get("tools") or []) if str(row.get("name")) == name]
    active = [row for row in rows if str(row.get("status")) == "active"]
    if len(active) == 1:
        return int(active[0]["version"])
    if not rows:
        raise SystemExit(f"注册表里没有工具 {name}")
    if not active:
        raise SystemExit(f"{name} 没有 active 版本；请带上版本号（agent tools list）")
    raise SystemExit(f"{name} 有多个 active 版本（{', '.join(str(r['version']) for r in active)}）；请带上版本号")


def cmd_tools(args: argparse.Namespace) -> int:
    if args.action == "list":
        if args.query:
            data = _ai_rpc(f"/tools?query={httpx.QueryParams({'query': args.query, 'k': args.limit})['query']}&k={args.limit}")
        else:
            data = _ai_rpc(f"/tools?k={args.limit}")
        table = _table(f"tools ({data.get('count', 0)})", ["name", "ver", "tier", "executor", "description"])
        for tool in data.get("tools", []):
            table.add_row(
                tool["name"],
                str(tool.get("version", "")),
                tool.get("tier", ""),
                tool.get("executor", ""),
                (tool.get("description") or "")[:70],
            )
        console.print(table)
        return 0
    if args.action == "inventory":
        console.print_json(json.dumps(_ai_rpc("/tools/inventory")))
        return 0
    if args.action == "show":
        payload = {"include_source": "true" if args.source else "false"}
        data = _ai_rpc(f"/tools/{args.name}?include_source={payload['include_source']}")
        console.print(Panel(json.dumps(data.get("params_schema"), indent=2, ensure_ascii=False), title=f"{data['name']} v{data['version']} arguments"))
        console.print(f"[bold]description:[/bold] {data['description']}")
        console.print(f"[bold]when to use:[/bold] {data['when_to_use']}")
        console.print(f"[bold]permissions:[/bold] {', '.join(data.get('permissions') or []) or 'none'}")
        console.print(f"[bold]executor:[/bold] {data['executor']}  [bold]status:[/bold] {data['status']}")
        console.print(f"[bold]runs:[/bold] {data.get('runs', 0)}  [bold]failures:[/bold] {data.get('failures', 0)}")
        if data.get("last_error"):
            console.print(f"[red]last error:[/bold] {data['last_error']}")
        if args.source and data.get("source"):
            console.print(Panel(data["source"], title="source", border_style="dim"))
        return 0
    if args.action == "runs":
        data = _ai_rpc(f"/tools/{args.name}/runs?limit={args.limit}")
        table = _table(f"runs of {args.name}", ["#", "ok", "ms", "error_code", "error"])
        for run in data.get("runs", []):
            table.add_row(str(run["id"]), "yes" if run["ok"] else "no", str(run["duration_ms"]), run.get("error_code") or "", (run.get("error") or "")[:60])
        console.print(table)
        return 0
    if args.action == "check":
        manifest = json.loads(Path(args.file).read_text(encoding="utf-8"))
        result = _ai_rpc("/tools/check", method="POST", payload={"manifest": manifest})
        console.print_json(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.action == "retire":
        version = _tool_version(args.name, args.version)
        body = _admin_rpc(
            "/admin/tools/retire", {"name": args.name, "version": version}, method="POST"
        )
        console.print(
            f"[green]已退休 {args.name} v{body.get('version', version)}[/green]"
            "（仅影响注册表，不写 .env）"
        )
        return 0
    if args.action == "delete":
        if args.purge and not args.confirm:
            raise SystemExit("硬删除（--purge）必须带 --confirm：它会连同该工具的历史记录一起清除")
        version = _tool_version(args.name, args.version)
        body = _admin_rpc(
            "/admin/tools/delete",
            {"name": args.name, "version": version, "purge": bool(args.purge), "confirm": bool(args.confirm)},
            method="POST",
        )
        done = "已彻底删除（purge）" if body.get("purged", args.purge) else "已删除"
        console.print(f"[green]{done} {args.name} v{body.get('version', version)}[/green]（仅影响注册表，不写 .env）")
        return 0
    err_console.print(f"unknown tools action {args.action}")
    return 2


# --------------------------------------------------------------------------- #
# sandbox / images / db
# --------------------------------------------------------------------------- #


def cmd_sandbox(args: argparse.Namespace) -> int:
    if args.action == "status":
        data = _control_rpc("sandbox.status", timeout=30.0)
        table = _table(
            f"sandbox pool (accel={data.get('accel')} warm={data.get('warm')} active={data.get('active')})",
            ["vm", "session", "state", "uptime", "commands"],
        )
        for vm in data.get("vms", []):
            table.add_row(
                vm["vm_id"], vm.get("session_id") or "-", vm["state"], f"{vm.get('uptime_s', 0):.0f}s", str(vm.get("commands_run", 0))
            )
        console.print(table)
        problems = data.get("image_problems") or []
        if problems:
            console.print(f"[red]image problems:[/red] {'; '.join(problems)}")
        return 0
    if args.action in {"reset", "stop", "start", "metrics"}:
        method = {
            "reset": "sandbox.reset",
            "stop": "sandbox.release",
            "start": "sandbox.acquire",
            "metrics": "sandbox.metrics",
        }[args.action]
        console.print_json(json.dumps(_control_rpc(method, {"session_id": args.session}, timeout=300.0)))
        return 0
    if args.action == "console":
        log_path = settings.console_dir / f"{args.vm}.log"
        if not log_path.exists():
            err_console.print(f"no console log at {log_path}")
            return 1
        console.print(log_path.read_text(errors="replace")[-20000:])
        return 0
    if args.action == "invoke":
        params = json.loads(args.params) if args.params else {}
        result = _control_rpc(
            "sandbox.invoke",
            {"session_id": args.session, "kind": "native", "method": args.method, "params": params},
            timeout=600.0,
        )
        console.print_json(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    err_console.print(f"unknown sandbox action {args.action}")
    return 2


def cmd_image(args: argparse.Namespace) -> int:
    if args.action == "verify":
        problems = settings.sandbox_image_problems()
        table = _table("sandbox image", ["artefact", "path", "size"])
        for path in (
            settings.base_kernel,
            settings.base_initrd,
            settings.base_rootfs,
            settings.blank_workspace,
        ):
            table.add_row(
                path.name,
                str(path),
                f"{path.stat().st_size / (1024 * 1024):.1f} MiB" if path.exists() else "-",
            )
        console.print(table)
        if problems:
            console.print(f"[red]{'; '.join(problems)}[/red]")
            return 1
        console.print("[green]sandbox image is complete[/green]")
        return 0
    if args.action == "build":
        if not sys.platform.startswith("linux"):
            err_console.print(
                "the sandbox image is built with debootstrap and must run inside the Debian platform VM.\n"
                "Run there: sudo deploy/sandbox/build-sandbox-image.sh"
            )
            return 2
        script = project_root() / "deploy" / "sandbox" / "build-sandbox-image.sh"
        if not script.exists():
            err_console.print(f"missing build script: {script}")
            return 2
        cmd = ["bash", str(script)] if os.geteuid() != 0 else ["bash", str(script)]
        console.print(f"[bold]building sandbox image[/bold] via {script}")
        completed = subprocess.run(cmd, cwd=str(project_root()))  # noqa: S603 - operator invoked build script
        return completed.returncode
    err_console.print(f"unknown image action {args.action}")
    return 2


def cmd_db(args: argparse.Namespace) -> int:
    async def run() -> int:
        from agent.registry import db as registry_db
        from agent.registry import service as registry_service

        if args.action in {"init", "seed"}:
            await registry_db.init_db(create=True)
            console.print("[green]schema and extensions ensured[/green]")
        if args.action == "seed":
            async with registry_db.session_scope() as session:
                outcome = await registry_service.seed_core_tools(session)
            table = _table("core tool seeding", ["tool", "action"])
            for name, action in sorted(outcome.items()):
                table.add_row(name, action)
            console.print(table)
        if args.action == "reembed":
            from agent.embeddings import get_embedder
            from agent.registry import repository as repo

            embedder = get_embedder()
            if not getattr(embedder, "available", False):
                err_console.print(
                    f"[red]embedder unavailable[/red] ({embedder.name}): install the local model or "
                    "set AGENT_EMBEDDING_BACKEND=dashscope before re-embedding"
                )
                return 1
            async with registry_db.session_scope() as session:
                pending = await repo.tools_missing_embedding(session, embedder.name)
                table = _table(f"re-embedding {len(pending)} tool(s) with {embedder.name}", ["tool", "result"])
                for name, version in pending:
                    record = await registry_service.resolve_tool(session, name, version, statuses=("active",))
                    if record is None:
                        table.add_row(f"{name} v{version}", "skipped (no longer active)")
                        continue
                    text = repo.build_search_text(record.name, record.description, record.when_to_use, record.tags)
                    try:
                        vector = await embedder.embed_one(text)
                    except Exception as exc:  # noqa: BLE001 - report and continue
                        table.add_row(f"{name} v{version}", f"[red]{type(exc).__name__}: {str(exc)[:60]}[/red]")
                        continue
                    await repo.set_embedding(session, name, version, vector, embedder.name)
                    table.add_row(f"{name} v{version}", "embedded")
                await session.commit()
            console.print(table)
            return 0

        if args.action == "ping":
            ok, detail = await registry_db.ping()
            console.print(f"{_ok(ok)} {detail}")
            return 0 if ok else 1
        if args.action in {"init", "seed"}:
            ok, detail = await registry_db.ping()
            console.print(f"[dim]{detail}[/dim]")
            return 0 if ok else 1
        await registry_db.dispose()
        return 0

    return asyncio.run(run())


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def cmd_doctor(args: argparse.Namespace) -> int:
    async def db_check() -> tuple[bool, str]:
        from agent.registry import db as registry_db

        ok, detail = await registry_db.ping()
        await registry_db.dispose()
        return ok, detail

    table = _table("agentbox doctor", ["check", "result", "detail"])
    table.add_row("python", _ok(sys.version_info >= (3, 11)), sys.version.split()[0])
    table.add_row("project root", Text(str(project_root()), style="dim"), "config: " + str(project_root() / ".env"))
    env_file = project_root() / ".env"
    table.add_row(".env", _ok(env_file.exists()), str(env_file))

    qemu_system = qemu_img = None
    try:
        qemu_system = settings.qemu_system
        qemu_img = settings.qemu_img
        table.add_row("qemu-system-x86_64", _ok(True), str(qemu_system))
        table.add_row("qemu-img", _ok(True), str(qemu_img))
    except FileNotFoundError as exc:
        table.add_row("qemu binaries", _ok(False), str(exc))

    from agent.control.host.base import detect_accel

    accel = detect_accel(settings.sandbox_accel)
    table.add_row("accelerator", Text(accel, style="bold"), "kvm>whpx>tcg (tcg is 5-20x slower)")

    problems = settings.sandbox_image_problems()
    table.add_row("sandbox image", _ok(not problems), "; ".join(problems) if problems else str(settings.sandbox_root))
    for path in (settings.base_kernel, settings.base_initrd, settings.base_rootfs, settings.blank_workspace):
        table.add_row(f"  {path.name}", _ok(path.exists()), f"{path.stat().st_size // 1024} KiB" if path.exists() else "-")

    # Which side are we on?  The host CLI cannot see the VM's PostgreSQL or its
    # .env, so those checks are "not applicable" rather than failures.
    host_side = _control_base() == settings.control_url_local.rstrip("/")

    def na(text: str) -> Text:
        return Text(text, style="dim")

    db_ok, db_detail = asyncio.run(db_check())
    if host_side and not db_ok:
        table.add_row("postgresql (in VM)", na("n/a"), "lives in the platform VM; run `agent doctor` there")
    else:
        table.add_row("postgresql (in VM)", _ok(db_ok), db_detail[:80])

    embedder = None
    try:
        from agent.embeddings import get_embedder

        embedder = get_embedder()
        available = bool(embedder.available)
        detail = f"{embedder.name} dim={embedder.dim}"
        if not available:
            detail += " (optional: keyword search fallback)"
        if host_side and not available:
            table.add_row("embedder (in VM)", na("n/a"), detail)
        else:
            table.add_row("embedder (in VM)", _ok(available), detail)
    except Exception as exc:  # noqa: BLE001
        table.add_row("embedder (in VM)", na(str(exc)[:80]), "optional")

    llm_detail = f"{settings.llm_model} @ {settings.llm_base_url}"
    if settings.llm_api_key:
        table.add_row("llm key (VM .env)", _ok(True), llm_detail)
    elif host_side:
        table.add_row("llm key (VM .env)", na("n/a"), "set AGENT_LLM_API_KEY in the VM's .env")
    else:
        table.add_row("llm key (VM .env)", _ok(False), llm_detail + " (set AGENT_LLM_API_KEY)")

    for label, url in (
        ("control plane (host)", f"{_control_base()}/health"),
        ("ai service (host -> VM)", f"http://127.0.0.1:{settings.ai_port}/health"),
    ):
        try:
            response = httpx.get(url, timeout=5.0)
            table.add_row(label, _ok(response.status_code == 200), f"{url} -> HTTP {response.status_code}")
        except httpx.HTTPError as exc:
            table.add_row(label, _ok(False), f"{url} ({type(exc).__name__})")

    console.print(table)
    console.print(
        "[dim]next steps:[/dim] agent db init && agent db seed  ·  "
        "[dim]build the sandbox image inside the Debian VM:[/dim] sudo deploy/sandbox/build-sandbox-image.sh"
    )
    return 0


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent", description="agentbox: QEMU sandboxed AI agent")
    parser.add_argument("--log-level", default=None, help="override AGENT_LOG_LEVEL")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run one of the two services in the foreground")
    serve.add_argument("role", choices=["ai", "control"])
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    chat = sub.add_parser("chat", help="interactive chat against the AI service")
    chat.add_argument("--session", default=None, help="resume a session id")
    chat.add_argument("--message", "-m", default=None, help="send one message and exit")
    chat.add_argument(
        "--log-style",
        choices=("ide", "plain", "json"),
        default="ide",
        help="how to render tool activity: ide panel (default), plain one-liners, json (one object per line)",
    )
    chat.add_argument("--log-lines", type=int, default=24, help="lines of each tool result shown in ide mode")
    chat.add_argument("--no-args", action="store_true", help="hide tool arguments in ide mode")
    chat.add_argument(
        "--voice",
        action="store_true",
        help="start in real-time voice mode: press Enter to speak, the answer is read back "
        "(needs a microphone and sounddevice; /voice-mode off leaves it)",
    )
    chat.add_argument(
        "--no-speak",
        dest="no_speak",
        action="store_true",
        help="voice mode stays silent: transcribe and answer, but do not speak the answer",
    )
    fold = chat.add_mutually_exclusive_group()
    fold.add_argument(
        "--log-fold",
        dest="log_fold",
        action="store_true",
        help="fold long tool output to the first few lines (default; /more shows the full text)",
    )
    fold.add_argument(
        "--no-log-fold",
        dest="log_fold",
        action="store_false",
        help="print whole tool results instead of folding them",
    )
    chat.set_defaults(func=cmd_chat, log_fold=True)


    tools = sub.add_parser("tools", help="inspect the tool registry")
    tools_sub = tools.add_subparsers(dest="action", required=True)
    tools_list = tools_sub.add_parser("list")
    tools_list.add_argument("--query", "-q", default=None, help="hybrid search query")
    tools_list.add_argument("--limit", "-n", type=int, default=20)
    tools_show = tools_sub.add_parser("show")
    tools_show.add_argument("name")
    tools_show.add_argument("--source", action="store_true")
    tools_runs = tools_sub.add_parser("runs")
    tools_runs.add_argument("name")
    tools_runs.add_argument("--limit", type=int, default=10)
    tools_check = tools_sub.add_parser("check", help="run the authoring gates on a manifest JSON file")
    tools_check.add_argument("file")
    tools_retire = tools_sub.add_parser("retire", help="retire a tool version (registry only, no .env write)")
    tools_retire.add_argument("name")
    tools_retire.add_argument("version", nargs="?", type=int, default=None)
    tools_delete = tools_sub.add_parser("delete", help="delete a tool version (registry only, no .env write)")
    tools_delete.add_argument("name")
    tools_delete.add_argument("version", nargs="?", type=int, default=None)
    tools_delete.add_argument(
        "--purge", action="store_true", help="hard delete: also clear the tool's history (needs --confirm)"
    )
    tools_delete.add_argument(
        "--confirm", action="store_true", help="confirm a hard delete (required with --purge)"
    )
    tools_sub.add_parser("inventory")
    tools.set_defaults(func=cmd_tools)

    sandbox = sub.add_parser("sandbox", help="control plane operations")
    sandbox_sub = sandbox.add_subparsers(dest="action", required=True)
    sandbox_sub.add_parser("status")
    for name in ("reset", "stop", "start", "metrics"):
        node = sandbox_sub.add_parser(name)
        node.add_argument("session")
    console_cmd = sandbox_sub.add_parser("console")
    console_cmd.add_argument("vm")
    invoke = sandbox_sub.add_parser("invoke", help="call a native guest RPC directly")
    invoke.add_argument("method")
    invoke.add_argument("--session", default="default")
    invoke.add_argument("--params", default="{}")
    sandbox.set_defaults(func=cmd_sandbox)

    image = sub.add_parser("image", help="sandbox base image")
    image_sub = image.add_subparsers(dest="action", required=True)
    image_sub.add_parser("build")
    image_sub.add_parser("verify")
    image.set_defaults(func=cmd_image)

    db = sub.add_parser("db", help="database maintenance")
    db_sub = db.add_subparsers(dest="action", required=True)
    for name in ("init", "seed", "ping"):
        db_sub.add_parser(name)
    # not in the loop above: argparse refuses a duplicate subparser name
    db_sub.add_parser("reembed", help="recompute tool embeddings with the configured model")
    db.set_defaults(func=cmd_db)

    doctor = sub.add_parser("doctor", help="environment self-check")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level)
    try:
        return int(args.func(args) or 0)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            err_console.print(exc.code)
            return 1
        return int(exc.code or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
