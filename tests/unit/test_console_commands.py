"""Console commands: parsing, what they send, and what must be refused.

The AI service's HTTP layer is stubbed, so these tests check the command logic: which
payload a command produces, which commands stay local, and that the dangerous ones
need explicit confirmation.  Persistence (the ssh rewrite of the VM's .env) is stubbed
the same way: no test may reach the network or the VM.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from rich.console import Console

from agent.ai import admin
from agent.cli.console import (
    DEFAULT_IMAGE_QUESTION,
    EFFORT_LEVELS,
    ConsoleState,
    SlashConsole,
)
from agent.cli.render import make_renderer
from agent.config import settings


class FakeResponse:
    """Enough of an httpx response for one stubbed call."""

    def __init__(self, status_code: int = 200, body: object | None = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeConsole(SlashConsole):
    """Records what would go to /admin/config and to the VM's .env, instead of calling them."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[dict] = []
        self.persisted: list[dict] = []
        self.persist_error: str | None = None
        self.values: dict = {
            "persona": "engineer",
            "net_enabled": False,
            "net_allow_hosts": "",
            "net_allow_ports": "80,443",
            "net_max_bytes": 8_000_000,
            "net_allow_private_hosts": False,
            "permission_tier": "safe",
            "llm_model": "deepseek-chat",
            "llm_effort": "",
        }

    def _admin(self, changes=None):  # noqa: ANN001
        if changes is not None:
            self.calls.append(dict(changes))
            self.values.update(changes)
        return {"ok": True, "effective": dict(self.values), "applied": [f"{k} -> {v}" for k, v in (changes or {}).items()]}

    def _persist(self, keys):  # noqa: ANN001
        if self.persist_error:
            raise RuntimeError(self.persist_error)
        self.persisted.append(dict(keys))


@pytest.fixture
def shell():
    console = Console(record=True, width=120, no_color=True, force_terminal=False)
    state = ConsoleState(log_style="ide", log_lines=24)
    return FakeConsole(console, state, console), console, state


def output(console: Console) -> str:
    return console.export_text()


def fake_llm(model: str = "deepseek-chat", effort: str = "", models: list[str] | None = None) -> FakeResponse:
    return FakeResponse(
        200,
        {
            "model": model,
            "vision_model": "deepseek-flash",
            "vision_enabled": True,
            "effort": effort,
            "available_models": models if models is not None else ["deepseek-flash", "deepseek-v4-pro"],
        },
    )


# --------------------------------------------------------------------------- net
def test_net_on_flips_the_switch(shell):
    sh, console, _ = shell
    assert sh.handle("/net on") is True
    assert sh.calls == [{"net_enabled": True}]
    assert "开" in output(console)


def test_net_allow_merges_with_the_existing_list(shell):
    sh, _, _ = shell
    sh.values["net_allow_hosts"] = "example.com"
    sh.handle("/net allow pypi.org,files.pythonhosted.org")
    assert sh.calls[-1] == {"net_allow_hosts": "example.com,files.pythonhosted.org,pypi.org"}


def test_net_deny_removes_a_host(shell):
    sh, _, _ = shell
    sh.values["net_allow_hosts"] = "a.com,b.com,c.com"
    sh.handle("/net deny b.com")
    assert sh.calls[-1] == {"net_allow_hosts": "a.com,c.com"}


def test_net_ports_and_private_warn(shell):
    sh, console, _ = shell
    sh.handle("/net ports 80,443,8443")
    assert sh.calls[-1] == {"net_allow_ports": "80,443,8443"}
    sh.handle("/net private on")
    assert sh.calls[-1] == {"net_allow_private_hosts": True}
    text = output(console)
    assert "SSRF" in text and "警告" in text


def test_net_on_with_empty_allowlist_explains_the_next_step(shell):
    sh, console, _ = shell
    sh.handle("/net on")
    assert "/net allow" in output(console)


def test_net_status_makes_no_change(shell):
    sh, console, _ = shell
    sh.handle("/net")
    assert sh.calls == []
    assert "白名单" in output(console)


def test_net_allow_without_arguments_is_refused(shell):
    sh, console, _ = shell
    sh.handle("/net allow")
    assert sh.calls == []
    assert "用法" in output(console)


# -------------------------------------------------------------------------- perm
def test_perm_safe_and_trusted_apply(shell):
    sh, _, _ = shell
    sh.handle("/perm trusted")
    assert sh.calls[-1] == {"permission_tier": "trusted"}
    sh.handle("/perm safe")
    assert sh.calls[-1] == {"permission_tier": "safe"}


def test_unrestricted_needs_the_confirmation_phrase(shell):
    sh, console, _ = shell
    sh.handle("/perm unrestricted")
    assert sh.calls == [], "without the phrase nothing may change"
    text = output(console)
    assert "危险" in text
    assert settings.host_exec_phrase in text


def test_unrestricted_with_a_wrong_phrase_is_refused(shell):
    sh, console, _ = shell
    sh.handle("/perm unrestricted LET-ME-IN")
    assert sh.calls == []
    assert "AGENT_HOST_EXEC_PHRASE" in output(console)


def test_unrestricted_with_the_right_phrase_arms_it(shell):
    sh, console, _ = shell
    sh.handle(f"/perm unrestricted {settings.host_exec_phrase}")
    assert sh.calls[-1] == {"permission_tier": "unrestricted"}
    assert "unrestricted 模式已启用" in output(console)


# ----------------------------------------------------------------------- persona
def test_persona_switch(shell):
    sh, _, _ = shell
    sh.handle("/persona teacher")
    assert sh.calls[-1] == {"persona": "teacher"}


def test_persona_list_uses_the_service(shell, monkeypatch):
    sh, console, _ = shell
    import httpx

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "active": "engineer", "personas": [{"name": "engineer", "source": "bundled"}]}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse())
    sh.handle("/persona")
    assert "engineer" in output(console)


# --------------------------------------------------------------------------- log
def test_log_commands_stay_local(shell):
    sh, console, state = shell
    sh.handle("/log json")
    sh.handle("/log lines 5")
    sh.handle("/log args off")
    assert sh.calls == [], "log settings are client side"
    assert (state.log_style, state.log_lines, state.show_args) == ("json", 5, False)
    assert "日志" in output(console)


def test_log_lines_is_clamped(shell):
    sh, _, state = shell
    sh.handle("/log lines 99999")
    assert state.log_lines == 500


def test_log_fold_toggles_and_stays_local(shell):
    sh, console, state = shell
    assert state.fold is True, "folding is on by default"
    sh.handle("/log fold off")
    assert state.fold is False
    sh.handle("/log fold on")
    assert state.fold is True
    sh.handle("/log fold maybe")  # bad argument: refused, unchanged
    assert state.fold is True
    assert "用法" in output(console)
    assert sh.calls == [] and sh.persisted == [], "/log is client side, nothing to persist"


def test_log_fold_reaches_the_live_renderer(shell):
    sh, _, state = shell
    state.renderer = make_renderer(Console(record=True, width=100, no_color=True, force_terminal=False), "ide", 24)
    sh.handle("/log fold off")
    assert state.renderer.fold is False
    sh.handle("/log fold on")
    assert state.renderer.fold is True


def test_log_status_mentions_folding(shell):
    sh, console, _ = shell
    sh.handle("/log")
    assert "折叠" in output(console)


# -------------------------------------------------------------------------- more
def _renderer_with_results(console: Console, *counts: int, fold: bool = True):
    """A renderer holding ``counts`` results, drawing into ``console`` like a real turn."""
    renderer = make_renderer(console, "ide", 24, fold=fold)
    for index, count in enumerate(counts, start=1):
        renderer.handle({"type": "tool_call", "index": index, "tool": "exec.run", "arguments": {}})
        body = "\n".join(f"line {i}" for i in range(count))
        renderer.handle(
            {
                "type": "tool_result",
                "index": index,
                "tool": "exec.run",
                "ok": True,
                "duration_ms": 5,
                "preview": json.dumps({"stdout": body}),
            }
        )
    console.export_text()  # drop the live rendering, keep the history
    return renderer, console


def test_more_reprints_the_full_output_of_the_last_result(shell):
    sh, console, state = shell
    state.renderer, _ = _renderer_with_results(console, 48)
    sh.handle("/more")
    out = output(console)
    assert "完整输出" in out
    assert "已折叠" not in out
    assert "line 47" in out, "/more must show what folding hid"
    assert sh.calls == [] and sh.persisted == []


def test_more_can_step_back(shell):
    sh, console, state = shell
    state.renderer, _ = _renderer_with_results(console, 20, 30)
    sh.handle("/more 2")
    out = output(console)
    assert "第 2 近的结果" in out
    assert "line 19" in out
    assert "line 29" not in out


def test_more_beyond_the_retained_window_is_explained(shell):
    sh, console, state = shell
    state.renderer, _ = _renderer_with_results(console, 10)
    sh.handle("/more 4")
    out = output(console)
    assert "只保留了最近 1 个" in out


def test_more_without_history_says_so(shell):
    sh, console, state = shell
    sh.handle("/more")
    assert "还没有可展开的工具结果" in output(console)


def test_more_rejects_a_non_number(shell):
    sh, console, state = shell
    state.renderer, _ = _renderer_with_results(console, 30)
    sh.handle("/more lots")
    assert "用法" in output(console)


# ------------------------------------------------------------------------- voice
@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / "note.wav"
    path.write_bytes(b"RIFF....WAVE-fake-audio")
    return path


def test_voice_sends_the_transcript_to_the_agent(shell, audio_file, monkeypatch):
    sh, console, state = shell
    seen: dict = {}

    def fake_post(url, content=None, headers=None, params=None, timeout=None):  # noqa: ANN001
        seen.update(url=url, content=content, headers=headers, params=params)
        return FakeResponse(200, {"ok": True, "text": "你好，世界", "language": "zh", "duration_s": 1.5, "model": "base"})

    monkeypatch.setattr("httpx.post", fake_post)
    assert sh.handle(f"/voice {audio_file}") is True

    assert seen["url"].endswith("/asr")
    assert seen["content"] == audio_file.read_bytes()
    assert seen["headers"]["Content-Type"] == "audio/wav"
    assert seen["headers"]["X-Agent-Token"] == settings.control_secret
    assert seen["params"] == {"language": "zh"}
    out = output(console)
    assert "识别到" in out and "你好，世界" in out
    assert state.pending_message == "你好，世界", "the transcript is the next message to the agent"


def test_voice_accepts_other_containers_by_extension(shell, tmp_path, monkeypatch):
    sh, _, state = shell
    seen: dict = {}
    path = tmp_path / "clip.webm"
    path.write_bytes(b"webm")

    def fake_post(url, content=None, headers=None, params=None, timeout=None):  # noqa: ANN001
        seen.update(headers=headers)
        return FakeResponse(200, {"ok": True, "text": "hi", "language": "en", "duration_s": 0.5, "model": "base"})

    monkeypatch.setattr("httpx.post", fake_post)
    sh.handle(f"/voice {path}")
    assert seen["headers"]["Content-Type"] == "audio/webm"


def test_voice_needs_a_file_argument(shell):
    sh, console, state = shell
    sh.handle("/voice")
    assert "用法" in output(console)
    assert state.pending_message is None


def test_voice_reports_a_missing_file(shell):
    sh, console, state = shell
    sh.handle("/voice C:/definitely/not/here.wav")
    out = output(console)
    assert "找不到音频文件" in out
    assert state.pending_message is None


def test_voice_reports_a_disabled_service(shell, audio_file, monkeypatch):
    sh, console, state = shell
    monkeypatch.setattr(
        "httpx.post",
        lambda *a, **k: FakeResponse(503, {"ok": False, "error": "ASR is disabled (AGENT_ASR_ENABLED=false)"}),
    )
    sh.handle(f"/voice {audio_file}")
    out = output(console)
    assert "语音识别不可用" in out
    assert "AGENT_ASR_ENABLED" in out
    assert state.pending_message is None


def test_voice_reports_an_unreachable_service(shell, audio_file, monkeypatch):
    sh, console, state = shell

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise __import__("httpx").ConnectError("connection refused")

    monkeypatch.setattr("httpx.post", boom)
    sh.handle(f"/voice {audio_file}")
    out = output(console)
    assert "无法连接 AI 服务" in out
    assert state.pending_message is None


def test_voice_with_silence_does_not_send_anything(shell, audio_file, monkeypatch):
    sh, console, state = shell
    monkeypatch.setattr(
        "httpx.post",
        lambda *a, **k: FakeResponse(200, {"ok": True, "text": "  ", "language": "zh", "duration_s": 0.1, "model": "b"}),
    )
    sh.handle(f"/voice {audio_file}")
    assert "没有识别到语音内容" in output(console)
    assert state.pending_message is None


def test_voice_is_not_persisted_and_makes_no_config_change(shell, audio_file, monkeypatch):
    sh, _, _ = shell
    monkeypatch.setattr(
        "httpx.post",
        lambda *a, **k: FakeResponse(200, {"ok": True, "text": "x", "language": "zh", "duration_s": 1, "model": "b"}),
    )
    sh.handle(f"/voice {audio_file}")
    assert sh.calls == [] and sh.persisted == []


# -------------------------------------------------------------------------- image
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 8


@pytest.fixture
def image_file(tmp_path):
    path = tmp_path / "solid.png"
    path.write_bytes(PNG)
    return path


def test_image_queues_the_file_for_the_next_message(shell, image_file):
    sh, console, state = shell
    assert sh.handle(f"/image {image_file}") is True
    assert len(state.pending_images) == 1
    queued = state.pending_images[0]
    assert queued["media_type"] == "image/png"
    assert base64.b64decode(queued["data_base64"]) == PNG
    assert state.pending_message == DEFAULT_IMAGE_QUESTION
    text = output(console)
    assert "已加入附图" in text and "solid.png" in text
    assert "1/4" in text
    assert sh.calls == [] and sh.persisted == [], "/image is client side until the turn is sent"


def test_image_carries_the_question_that_followed_the_path(shell, image_file):
    sh, console, state = shell
    sh.handle(f"/image {image_file} 这是什么颜色？")
    assert state.pending_message == "这是什么颜色？"
    assert "附带的问题：这是什么颜色？" in output(console)


def test_image_accepts_older_jpeg_spelling(shell, tmp_path):
    sh, _, state = shell
    path = tmp_path / "photo.jpeg"
    path.write_bytes(b"\xff\xd8\xff\xe0jpeg-bytes")
    sh.handle(f"/image {path}")
    assert state.pending_images[0]["media_type"] == "image/jpeg"


def test_image_accepts_webp_and_gif(shell, tmp_path):
    sh, _, state = shell
    webp = tmp_path / "a.webp"
    webp.write_bytes(b"RIFFxxxxWEBP")
    gif = tmp_path / "b.gif"
    gif.write_bytes(b"GIF89a")
    sh.handle(f"/image {webp}")
    sh.handle(f"/image {gif}")
    assert [item["media_type"] for item in state.pending_images] == ["image/webp", "image/gif"]


def test_image_refuses_an_unsupported_format(shell, tmp_path):
    sh, console, state = shell
    path = tmp_path / "scan.bmp"
    path.write_bytes(b"BM" + b"\x00" * 20)
    sh.handle(f"/image {path}")
    text = output(console)
    assert "不支持的图片格式" in text
    assert ".png" in text and ".gif" in text, "the refusal must list what is allowed"
    assert state.pending_images == []


def test_image_reports_a_missing_file(shell):
    sh, console, state = shell
    sh.handle("/image C:/definitely/not/here.png")
    assert "找不到图片文件" in output(console)
    assert state.pending_images == []


def test_image_reports_an_empty_file(shell, tmp_path):
    sh, console, state = shell
    path = tmp_path / "empty.png"
    path.write_bytes(b"")
    sh.handle(f"/image {path}")
    assert "图片文件是空的" in output(console)
    assert state.pending_images == []


def test_image_reports_the_size_limit(shell, tmp_path, monkeypatch):
    sh, console, state = shell
    path = tmp_path / "big.png"
    path.write_bytes(PNG)
    monkeypatch.setattr(settings, "llm_max_image_bytes", 4)
    sh.handle(f"/image {path}")
    text = output(console)
    assert "图片过大" in text
    assert "AGENT_LLM_MAX_IMAGE_BYTES" in text
    assert state.pending_images == []


def test_image_enforces_the_count_limit(shell, tmp_path, monkeypatch):
    sh, console, state = shell
    monkeypatch.setattr(settings, "llm_max_images", 2)
    paths = []
    for index in range(3):
        path = tmp_path / f"{index}.png"
        path.write_bytes(PNG)
        paths.append(path)
    sh.handle(f"/image {paths[0]} {paths[1]}")
    assert len(state.pending_images) == 2
    sh.handle(f"/image {paths[2]}")
    text = output(console)
    assert "图片数量超过上限" in text
    assert "AGENT_LLM_MAX_IMAGES" in text
    assert len(state.pending_images) == 2, "the rejected image must not be queued"


def test_image_needs_an_argument(shell):
    sh, console, state = shell
    sh.handle("/image")
    assert "用法" in output(console)
    assert state.pending_images == []


def test_images_lists_and_image_clear_empties_the_queue(shell, image_file):
    sh, console, state = shell
    sh.handle("/image C:/nope/none.png")
    output(console)
    sh.handle("/images")
    assert "还没有排队的图片" in output(console)

    sh.handle(f"/image {image_file}")
    output(console)
    sh.handle("/images")
    assert "solid.png" in output(console)

    sh.handle("/image-clear")
    assert "已清空 1 张" in output(console)
    assert state.pending_images == []


def test_image_path_without_pending_files_is_not_persisted(shell, image_file):
    sh, _, _ = shell
    sh.handle(f"/image {image_file}")
    sh.handle("/image-clear")
    assert sh.persisted == []


# -------------------------------------------------------------------------- model
def test_model_lists_the_service_models(shell, monkeypatch):
    sh, console, _ = shell
    monkeypatch.setattr("httpx.get", lambda *a, **k: fake_llm())
    sh.handle("/model")
    text = output(console)
    assert "deepseek-chat" in text
    assert "deepseek-flash" in text and "deepseek-v4-pro" in text
    assert sh.calls == [] and sh.persisted == []


def test_model_without_the_service_still_lists_the_known_models(shell, monkeypatch):
    sh, console, _ = shell
    import httpx

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.get", boom)
    sh.handle("/model")
    text = output(console)
    assert "无法连接 AI 服务" in text
    assert "deepseek-flash" in text and "deepseek-v4-pro" in text


def test_model_switches_and_persists(shell, monkeypatch):
    sh, console, _ = shell
    monkeypatch.setattr("httpx.get", lambda *a, **k: fake_llm())
    sh.handle("/model deepseek-flash")
    assert sh.calls[-1] == {"llm_model": "deepseek-flash"}
    assert sh.persisted, "/model is a runtime change and must be persisted"
    written = sh.persisted[-1]
    assert written["AGENT_LLM_MODEL"] == "deepseek-flash"
    assert written["AGENT_PERSONA"] == "engineer", "the other mutable keys ride along"
    assert "已写入 .env（重启后仍生效）" in output(console)


def test_model_rejects_a_name_the_service_does_not_offer(shell, monkeypatch):
    sh, console, _ = shell
    monkeypatch.setattr("httpx.get", lambda *a, **k: fake_llm())
    sh.handle("/model gpt-5-turbo")
    text = output(console)
    assert "未知模型" in text
    assert "deepseek-flash" in text, "the refusal must list the available models"
    assert sh.calls == []


def test_model_says_when_the_mutable_key_is_not_writable(shell, monkeypatch):
    sh, console, _ = shell
    monkeypatch.setattr("httpx.get", lambda *a, **k: fake_llm())

    def refuse(changes):  # noqa: ANN001
        raise RuntimeError("cannot change llm_model; mutable keys: persona, net_enabled")

    monkeypatch.setattr(sh, "_apply", refuse)
    sh.handle("/model deepseek-flash")
    text = output(console)
    assert "暂不支持运行时修改" in text
    assert "llm_model" in text
    assert "restart" in text, "the message must say how to change it permanently"
    assert sh.persisted == [], "a refused change must not be written to .env"


def test_model_switch_works_while_the_list_is_unavailable(shell, monkeypatch):
    sh, _, _ = shell
    import httpx

    monkeypatch.setattr("httpx.get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    sh.handle("/model deepseek-v4-pro")
    assert sh.calls[-1] == {"llm_model": "deepseek-v4-pro"}


# -------------------------------------------------------------------------- think
def test_think_shows_the_current_effort(shell, monkeypatch):
    sh, console, _ = shell
    monkeypatch.setattr("httpx.get", lambda *a, **k: fake_llm(effort="high"))
    sh.handle("/think")
    text = output(console)
    assert "思考强度：high" in text
    assert "不稳定" in text
    assert sh.calls == [] and sh.persisted == []


def test_think_shows_the_gateway_default_when_empty(shell, monkeypatch):
    sh, console, _ = shell
    monkeypatch.setattr("httpx.get", lambda *a, **k: fake_llm(effort=""))
    sh.handle("/think")
    assert "网关默认" in output(console)


@pytest.mark.parametrize("level", EFFORT_LEVELS)
def test_think_sets_each_level_and_persists(shell, monkeypatch, level):
    sh, console, _ = shell
    monkeypatch.setattr("httpx.get", lambda *a, **k: fake_llm())
    sh.handle(f"/think {level}")
    assert sh.calls[-1] == {"llm_effort": level}
    written = sh.persisted[-1]
    assert written["AGENT_LLM_EFFORT"] == level
    assert written["AGENT_LLM_MODEL"] == "deepseek-chat", "the model rides along"
    text = output(console)
    assert "该参数在当前网关效果不稳定" in text, "the success note must stay honest"


@pytest.mark.parametrize("word", ["default", "off", "none", "clear"])
def test_think_can_be_cleared(shell, monkeypatch, word):
    sh, _, _ = shell
    monkeypatch.setattr("httpx.get", lambda *a, **k: fake_llm())
    sh.values["llm_effort"] = "high"
    sh.handle(f"/think {word}")
    assert sh.calls[-1] == {"llm_effort": ""}, "clearing means sending no effort field at all"


def test_think_rejects_an_unknown_level(shell, monkeypatch):
    sh, console, _ = shell
    monkeypatch.setattr("httpx.get", lambda *a, **k: fake_llm())
    sh.handle("/think turbo")
    text = output(console)
    assert "未知强度" in text
    assert "low" in text and "high" in text and "max" in text
    assert sh.calls == []


def test_think_says_when_the_mutable_key_is_not_writable(shell, monkeypatch):
    sh, console, _ = shell
    monkeypatch.setattr("httpx.get", lambda *a, **k: fake_llm())

    def refuse(changes):  # noqa: ANN001
        raise RuntimeError("cannot change llm_effort; mutable keys: persona")

    monkeypatch.setattr(sh, "_apply", refuse)
    sh.handle("/think low")
    text = output(console)
    assert "暂不支持运行时修改" in text and "llm_effort" in text
    assert sh.persisted == []


# ------------------------------------------------------------------- persistence
def test_a_change_is_written_to_the_env_immediately(shell):
    sh, console, _ = shell
    sh.handle("/net on")
    assert sh.persisted, "a runtime change must be persisted right away"
    keys = sh.persisted[-1]
    assert keys["AGENT_NET_ENABLED"] == "1"
    assert "已写入 .env（重启后仍生效）" in output(console)


def test_every_mutable_command_persists(shell):
    sh, _, _ = shell
    sh.handle("/perm trusted")
    sh.handle("/net allow pypi.org")
    sh.handle("/persona teacher")
    sh.handle("/net ports 80,443,8443")
    assert len(sh.persisted) == 4
    assert sh.persisted[-1]["AGENT_NET_ALLOW_PORTS"] == "80,443,8443"
    assert sh.persisted[0]["AGENT_PERMISSION_TIER"] == "trusted"
    assert sh.persisted[1]["AGENT_NET_ALLOW_HOSTS"] == "pypi.org"


def test_read_only_commands_do_not_persist(shell):
    sh, _, _ = shell
    # /sandbox is read-only too, but it needs the control plane, so it is not in this list
    for line in ("/net", "/persona", "/log", "/clear", "/session", "/net status", "/more"):
        sh.handle(line)
    assert sh.persisted == [], f"read-only commands must not rewrite .env: {sh.persisted}"


def test_a_failed_persist_warns_but_keeps_the_runtime_change(shell):
    sh, console, _ = shell
    sh.persist_error = "缺少 ssh 客户端或 var/vm_key（请改在 VM 内运行）"
    sh.handle("/net on")
    out = output(console)
    assert "无法持久化" in out or "警告" in out
    assert "只在当前运行有效" in out
    assert sh.values["net_enabled"] is True, "the runtime change must survive the failed write"


def test_save_still_works_as_a_manual_resync(shell):
    sh, console, _ = shell
    sh.handle("/save")
    assert len(sh.persisted) == 1
    assert "已保存" in output(console)


# ----------------------------------------------------------------- misc commands
def test_unknown_command_suggests_help(shell):
    sh, console, _ = shell
    assert sh.handle("/frobnicate") is True
    assert "未知命令" in output(console)


def test_exit_and_new_and_session(shell):
    sh, console, state = shell
    sh.handle("/session")
    assert "会话" in output(console)
    sh.handle("/new")
    assert state.new_session is True and state.session_id is None
    sh.handle("/exit")
    assert state.exit_requested is True


def test_non_commands_are_not_consumed(shell):
    sh, _, _ = shell
    assert sh.handle("hello there") is False


# -------------------------------------------------------------- admin.apply rules
def test_admin_rejects_unknown_keys_without_changing_anything():
    before = admin.effective()
    with pytest.raises(admin.ConfigError, match="cannot change"):
        admin.apply({"definitely_not_a_setting": 1})
    assert admin.effective() == before


def test_admin_coerces_and_reports_changes():
    effective, applied = admin.apply({"net_enabled": "on", "search_k": "7"})
    assert effective["net_enabled"] is True
    assert effective["search_k"] == 7
    assert any("net_enabled" in line for line in applied)
    admin.apply({"net_enabled": False, "search_k": 5})


def test_admin_validates_persona_names():
    with pytest.raises(admin.ConfigError, match="unknown persona"):
        admin.apply({"persona": "ghost"})
    with pytest.raises(admin.ConfigError, match="boolean"):
        admin.apply({"net_enabled": "perhaps"})


def test_admin_rejects_a_bad_tier():
    with pytest.raises(admin.ConfigError):
        admin.apply({"permission_tier": "god"})
    assert admin.effective()["permission_tier"] in {"safe", "trusted", "unrestricted"}


# ------------------------------------------------------------------ /tools (registry)
# `/tools` talks to the AI service's registry routes (GET /admin/tools,
# POST /admin/tools/retire, POST /admin/tools/delete) and never to /admin/config, so these
# tests stub httpx: they assert the exact URL, method and body the console sends.


@pytest.fixture
def registry(monkeypatch):
    """A recording stand-in for the AI service's /admin/tools* routes."""
    sent: list[dict] = []
    rows = [
        {"name": "fs.read", "version": 1, "status": "active", "tier": "core", "created_by": "seed"},
        {"name": "word_count", "version": 2, "status": "active", "tier": "generated", "created_by": "model"},
        {"name": "word_count", "version": 1, "status": "retired", "tier": "generated", "created_by": "model"},
    ]

    def get(url, headers=None, timeout=None):  # noqa: ANN001
        sent.append({"method": "GET", "url": url})
        return FakeResponse(200, {"ok": True, "count": len(rows), "tools": rows})

    def post(url, headers=None, json=None, timeout=None):  # noqa: ANN001
        sent.append({"method": "POST", "url": url, "json": json})
        name = (json or {}).get("name")
        version = (json or {}).get("version")
        if "retire" in url:
            return FakeResponse(200, {"ok": True, "name": name, "version": version, "retired": 1, "remaining": []})
        return FakeResponse(
            200,
            {"ok": True, "name": name, "version": version, "purged": bool((json or {}).get("purge")), "deleted": 1},
        )

    monkeypatch.setattr("httpx.get", get)
    monkeypatch.setattr("httpx.post", post)
    return sent


def test_tools_list_groups_by_tier_and_marks_retired(shell, registry):
    sh, console, _ = shell

    assert sh.handle("/tools") is True
    out = output(console)

    assert "fs.read" in out and "word_count" in out
    assert "retired" in out, "a retired version is marked, not hidden"
    assert "retire" in out and "delete" in out, "the help line shows how to change things"
    assert registry == [{"method": "GET", "url": f"http://127.0.0.1:{settings.ai_port}/admin/tools"}], (
        "listing is a single read-only GET"
    )


def test_tools_retire_uses_the_single_active_version(shell, registry):
    sh, console, _ = shell

    assert sh.handle("/tools retire word_count") is True

    call = registry[-1]
    assert call["method"] == "POST"
    assert call["url"].endswith("/admin/tools/retire")
    assert call["json"] == {"name": "word_count", "version": 2}, "no version -> the only active one"
    assert "已退休 word_count v2" in output(console)


def test_tools_retire_refuses_an_ambiguous_version(shell, registry, monkeypatch):
    sh, console, _ = shell
    rows = registry  # the fixture's rows; add a second active version via a fresh response

    def get(url, headers=None, timeout=None):  # noqa: ANN001
        rows.append({"method": "GET", "url": url})
        return FakeResponse(
            200,
            {
                "ok": True,
                "tools": [
                    {"name": "word_count", "version": 1, "status": "active", "tier": "generated"},
                    {"name": "word_count", "version": 2, "status": "active", "tier": "generated"},
                ],
            },
        )

    monkeypatch.setattr("httpx.get", get)
    assert sh.handle("/tools retire word_count") is True
    assert "多个 active 版本" in output(console), "the operator is told to pick a version"
    assert not [call for call in rows if call.get("method") == "POST"], "nothing was retired"


def test_tools_delete_purge_needs_the_name_typed_back(shell, registry):
    sh, console, _ = shell
    answers = iter(["word_count"])
    sh.console.input = lambda prompt="": next(answers)  # noqa: ARG005

    assert sh.handle("/tools delete word_count --purge") is True

    call = registry[-1]
    assert call["url"].endswith("/admin/tools/delete")
    # the API refuses a hard purge without confirm=true, and the console must send it --
    # an operator "delete does nothing" report is exactly what a missing flag looks like
    assert call["json"] == {"name": "word_count", "version": 2, "purge": True, "confirm": True}
    assert "已彻底删除" in output(console)


def test_tools_soft_delete_sends_no_purge_and_no_confirm(shell, registry):
    sh, console, _ = shell

    assert sh.handle("/tools delete word_count") is True

    call = registry[-1]
    assert call["json"] == {"name": "word_count", "version": 2, "purge": False, "confirm": True}
    assert "已删除" in output(console) and "彻底" not in output(console)


def test_tools_delete_purge_aborts_when_the_name_does_not_match(shell, registry):
    sh, console, _ = shell
    sh.console.input = lambda prompt="": "not-the-name"  # noqa: ARG005

    assert sh.handle("/tools delete word_count --purge") is True
    assert not [call for call in registry if call.get("method") == "POST"], "a refused purge sends nothing"
    assert "已取消" in output(console)


def test_tools_rejects_an_unknown_action(shell, registry):
    sh, console, _ = shell

    assert sh.handle("/tools explode") is True
    assert "用法" in output(console)
    assert registry == [], "a bad action touches nothing"


def test_tools_reports_an_unauthorized_registry(shell, monkeypatch):
    sh, console, _ = shell
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse(401, {"ok": False, "error": "unauthorized"}))

    assert sh.handle("/tools") is True
    assert "AGENT_CONTROL_SECRET" in output(console), "the refusal names the setting to check"


def test_tools_help_and_completion_are_discoverable():
    from agent.cli.console import COMMAND_HELP, complete

    assert "/tools" in COMMAND_HELP["tools"]
    assert "/tools retire" in COMMAND_HELP["tools"]
    assert "/tools delete" in COMMAND_HELP["tools"]
    assert complete("/tools ") == ["/tools list", "/tools retire", "/tools delete"]


# --------------------------------------------------------------------- /get (host pull)
# `/get` is the console twin of the `fs.pull` tool: it reads the file out of the session
# sandbox (`sandbox.invoke` -> guest `fs.read`) and hands the bytes to the control plane
# (`host.pull.write`).  It must never go through the AI service's `/admin/config`: that
# endpoint is for settings, and the file has to come out even when the service is down.


def test_get_hands_the_bytes_to_the_control_plane_and_never_admin_config(shell, tmp_path, monkeypatch):
    sh, console, state = shell
    state.session_id = "s-test"
    payload = b"\x89PNG\r\n\x1a\n" + b"7" * 300
    calls: list[tuple[str, dict, float]] = []
    opened: list[list[str]] = []

    def fake_rpc(method, params=None, timeout=120.0):  # noqa: ANN001
        calls.append((method, dict(params or {}), timeout))
        if method == "sandbox.invoke":
            # exactly what ``_control_rpc`` hands back for a successful guest ``fs.read``:
            # the SandboxInvokeResult, whose "result" is the handler payload (no "ok").
            return {
                "ok": True,
                "result": {
                    "path": "/workspace/plot.png",
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "truncated": False,
                    "data_b64": base64.b64encode(payload).decode("ascii"),
                },
                "duration_ms": 7,
                "vm_id": "vm-test",
            }
        return {"ok": True, "path": str(tmp_path / "pulled" / "plot.png"), "bytes": len(payload)}

    monkeypatch.setattr("agent.cli.main._control_rpc", fake_rpc)
    monkeypatch.setattr("subprocess.run", lambda argv, **kw: opened.append(list(argv)))

    assert sh.handle("/get /workspace/plot.png") is True

    methods = [method for method, _, _ in calls]
    assert methods == ["sandbox.invoke", "host.pull.write"], f"unexpected RPCs: {methods}"
    assert not any("admin/config" in str(call) for call in calls)
    read = calls[0][1]
    assert read["session_id"] == "s-test" and read["method"] == "fs.read"
    assert read["params"]["path"] == "/workspace/plot.png" and read["params"]["binary"] is True
    write = calls[1][1]
    assert write["dest"] == "plot.png" and write["append"] is False and write["overwrite"] is False
    assert base64.b64decode(write["data_b64"]) == payload
    text = output(console)
    assert "plot.png" in text and "308 B" in text, "the operator must see where the file landed"
    assert opened and "Start-Process" in opened[0], "a .png must be opened with the default viewer"
    assert sh.calls == [], "the file path must not touch /admin/config"


def test_get_refuses_a_path_outside_the_workspace_and_a_dest_that_escapes(shell, monkeypatch):
    sh, console, _ = shell
    calls: list[str] = []
    monkeypatch.setattr(
        "agent.cli.main._control_rpc",
        lambda method, params=None, timeout=120.0: calls.append(method) or {},  # noqa: ARG005
    )

    assert sh.handle("/get /etc/passwd") is True
    assert sh.handle("/get /workspace/../../etc/passwd") is True
    assert sh.handle("/get /workspace/a.png ../escape.png") is True
    assert calls == [], "a refused /get must not reach the control plane"
    assert "已保存" not in output(console)


def test_get_accepts_the_live_fs_read_payload_shape(shell, tmp_path, monkeypatch):
    """Regression (live: ``/get /workspace/acg.jpg`` said 沙箱读不到).

    The gateway's ``fs.read`` answers with ``path``/``size``/``sha256``/``truncated``/
    ``data_b64`` and *no* top-level ``ok``.  The console unwrapped one level too many, so
    that good payload was read as a failed invoke and no host file was ever written.
    """
    sh, console, state = shell
    state.session_id = "s-test"
    # a real JPEG header, truncated on purpose; the reported size/sha256 are the live ones
    blob_b64 = "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    payload = base64.b64decode(blob_b64)
    calls: list[tuple[str, dict]] = []
    opened: list[list[str]] = []

    def fake_rpc(method, params=None, timeout=120.0):  # noqa: ANN001
        calls.append((method, dict(params or {})))
        if method == "sandbox.invoke":
            return {
                "ok": True,
                "result": {
                    "path": "/workspace/acg.jpg",
                    "size": 232268,
                    "sha256": "6c5ce158eb785a6484021bc6b4f8cae62f155225b5f3b130668580e95b497190",
                    "truncated": False,
                    "data_b64": blob_b64,
                },
                "duration_ms": 9,
                "vm_id": "vm-test",
            }
        return {"ok": True, "path": str(tmp_path / "pulled" / "acg.jpg"), "bytes": len(payload)}

    monkeypatch.setattr("agent.cli.main._control_rpc", fake_rpc)
    monkeypatch.setattr("subprocess.run", lambda argv, **kw: opened.append(list(argv)))

    assert sh.handle("/get /workspace/acg.jpg") is True

    assert [method for method, _ in calls] == ["sandbox.invoke", "host.pull.write"]
    assert base64.b64decode(calls[1][1]["data_b64"]) == payload, "the bytes must reach host.pull.write"
    text = output(console)
    assert "沙箱读不到" not in text, "a successful fs.read must not be reported as unreadable"
    assert "已保存" in text and "acg.jpg" in text and hashlib.sha256(payload).hexdigest()[:16] in text
    assert opened and "Start-Process" in opened[0]

