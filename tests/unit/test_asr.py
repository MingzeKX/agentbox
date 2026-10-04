"""Local speech-to-text: the transcriber wrapper, its failure modes and its guardrails.

No real inference runs here -- ctranslate2 is heavy and the model is only present on the
platform VM.  What is tested is the contract the endpoint and the console rely on:
never raise, say why it cannot transcribe, and keep the audio bytes out of the
filesystem.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent import asr
from agent.config import settings

# ------------------------------------------------------------------ transcriber


@pytest.fixture
def transcriber() -> asr.Transcriber:
    return asr.Transcriber(model_name="test-model", compute_type="int8", max_seconds=30.0)


@pytest.mark.asyncio
async def test_empty_audio_is_rejected(transcriber):
    result = await transcriber.transcribe(b"")
    assert result["ok"] is False
    assert "empty" in result["error"]
    assert result["model"] == "test-model"


@pytest.mark.asyncio
async def test_disabled_transcriber_says_so(transcriber, monkeypatch):
    monkeypatch.setattr(settings, "asr_enabled", False)
    result = await transcriber.transcribe(b"RIFF....")
    assert result["ok"] is False
    assert "disabled" in result["error"]
    monkeypatch.setattr(settings, "asr_enabled", True)


@pytest.mark.asyncio
async def test_missing_library_is_reported_not_raised(transcriber):
    transcriber._import_ok = False
    transcriber._load_error = "faster-whisper is not importable (ImportError: nope)"
    result = await transcriber.transcribe(b"RIFF....")
    assert result["ok"] is False
    assert "not importable" in result["error"]
    assert transcriber.available is False
    assert "not importable" in transcriber.unavailable_reason()


@pytest.mark.asyncio
async def test_successful_transcription_plumbing(transcriber, monkeypatch):
    transcriber._import_ok = True

    def fake_sync(audio: bytes, language: str | None) -> dict:
        assert audio == b"RIFF...."
        assert language == "zh"
        return {"text": "你好世界", "language": "zh", "duration_s": 1.5, "segments": 1}

    monkeypatch.setattr(transcriber, "_transcribe_sync", fake_sync)
    result = await transcriber.transcribe(b"RIFF....", language="zh", filename="hello.wav")
    assert result["ok"] is True
    assert result["text"] == "你好世界"
    assert result["language"] == "zh"
    assert result["duration_s"] == 1.5
    assert result["model"] == "test-model"


@pytest.mark.asyncio
async def test_exceptions_never_escape(transcriber, monkeypatch):
    transcriber._import_ok = True

    def boom(audio: bytes, language: str | None) -> dict:  # noqa: ARG001
        raise RuntimeError("ctranslate2 exploded")

    monkeypatch.setattr(transcriber, "_transcribe_sync", boom)
    result = await transcriber.transcribe(b"RIFF....")
    assert result["ok"] is False
    assert "ctranslate2 exploded" in result["error"]


@pytest.mark.asyncio
async def test_too_long_audio_is_refused_with_the_reason(transcriber, monkeypatch):
    transcriber._import_ok = True

    def too_long(audio: bytes, language: str | None) -> dict:  # noqa: ARG001
        raise asr.AudioTooLong("audio is 900.0s, the limit is 30.0s (AGENT_ASR_MAX_SECONDS)")

    monkeypatch.setattr(transcriber, "_transcribe_sync", too_long)
    result = await transcriber.transcribe(b"RIFF....")
    assert result["ok"] is False
    assert "AGENT_ASR_MAX_SECONDS" in result["error"]


def test_singleton_is_reused():
    asr.set_transcriber(None)
    first = asr.get_transcriber()
    assert asr.get_transcriber() is first
    asr.set_transcriber(None)


def test_language_normalisation():
    assert asr._normalise_language(None) in (None, "")
    assert asr._normalise_language("zh") == "zh"
    assert asr._normalise_language("auto") in (None, "")


# --------------------------------------------------------------------- guardrail


def test_asr_module_does_not_touch_forbidden_apis():
    """The AI service must not read files or spawn processes; it gets bytes over HTTP.

    Same rule as tests/unit/test_isolation_guard.py, applied locally so a future edit
    cannot quietly turn ASR into a file-reading or process-spawning component.
    """
    source = (Path(__file__).resolve().parents[2] / "src" / "agent" / "asr.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned_imports = {"subprocess", "socket", "ctypes", "shutil", "importlib"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned_imports, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned_imports, node.module
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            assert name != "open", "asr.py must not open files"
