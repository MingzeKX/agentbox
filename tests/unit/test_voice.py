"""Real-time voice mode: VAD, WAV framing, SAPI command building, and the loop.

No test may touch a microphone, a speaker or the network.  The VAD and the WAV writer are
pure functions over synthetic int16 buffers, the TTS side is a pure command builder (the
runner is injected and raises if it is ever called), and the loop is driven by a fake
recorder with a stubbed transcriber and speaker.

Block arithmetic used throughout: 20 ms at 16 kHz = 320 samples = 640 bytes, so 0.2 s is
10 blocks, 1.0 s is 50 blocks.  (The VAD itself is block-size agnostic -- it works on
whatever a device hands it -- so the tests are free to pick a convenient size.)
"""

from __future__ import annotations

import array
import math
import random
import struct
import sys

import pytest
from rich.console import Console

from agent.cli import voice as voice_module
from agent.cli.console import ConsoleState, SlashConsole
from agent.cli.voice import (
    CONTINUE_PROMPT,
    RECORDING_MESSAGE,
    SAMPLE_RATE,
    SPEAKING_MESSAGE,
    TALK_KEY,
    TALK_KEY_ALT,
    TALK_SENTINEL,
    TRANSCRIBING_MESSAGE,
    VAD_REASONS,
    EnergyVad,
    SpeechHandle,
    TalkKey,
    VadConfig,
    VoiceError,
    _linear_resample,
    build_speak_command,
    detect_utterance,
    devices_table,
    flatten_for_speech,
    list_input_devices,
    pack_wav,
    resample_int16,
    rms_int16,
    say_with_barge_in,
    silence_message,
    speak,
    truncate_for_speech,
    voice_loop,
)
from agent.config import settings

RATE = SAMPLE_RATE
#: The VAD judges whatever blocks it is handed, so the tests pick a 20 ms block to keep
#: the arithmetic exact (0.2 s = 10 blocks, 1.0 s = 50 blocks, 640 bytes per block).
BLOCK_MS = 20
BLOCK_SAMPLES = RATE * BLOCK_MS // 1000
BLOCK_BYTES = BLOCK_SAMPLES * 2


# --------------------------------------------------------------------------- audio


def tone(samples: int, amplitude: float = 0.5, frequency: int = 220) -> bytes:
    """A sine ``amplitude`` full-scale, as little-endian int16."""
    out = array.array("h")
    for index in range(samples):
        out.append(int(amplitude * 32767 * math.sin(2 * math.pi * frequency * index / RATE)))
    return out.tobytes()


def silence(blocks: int) -> list[bytes]:
    return [b"\x00\x00" * BLOCK_SAMPLES for _ in range(blocks)]


def speech(blocks: int, amplitude: float = 0.5) -> list[bytes]:
    return [tone(BLOCK_SAMPLES, amplitude) for _ in range(blocks)]


def room_noise(blocks: int, amplitude: int = 100) -> list[bytes]:
    """Low-level noise (below ``voice_vad_floor`` by default), as a real room would have."""
    rng = random.Random(20240517)
    return [
        array.array("h", [rng.randint(-amplitude, amplitude) for _ in range(BLOCK_SAMPLES)]).tobytes()
        for _ in range(blocks)
    ]


def config(**overrides) -> VadConfig:
    """Fast VAD settings: 0.2 s calibration/silence, 1.0 s cap, 10-block granularity."""
    base = {
        "sample_rate": RATE,
        "silence_s": 0.2,
        "max_s": 1.0,
        "floor": 0.012,
        "calibration_s": 0.2,
        "min_speech_s": 0.0,
        "preroll_s": 0.0,
    }
    base.update(overrides)
    return VadConfig(**base)


def consume(vad: EnergyVad, blocks: list[bytes]) -> int:
    """Feed blocks until the VAD stops asking; returns how many it took."""
    taken = 0
    for block in blocks:
        taken += 1
        if not vad.push(block):
            break
    return taken


# ----------------------------------------------------------------------------- VAD


def test_vad_hears_only_silence_and_returns_nothing():
    """1. A minute of quiet must not produce an utterance (and never stops early)."""
    blocks = silence(40)
    vad = EnergyVad(config())
    taken = consume(vad, blocks)
    result = vad.finish()

    assert taken == 40, "silence must not end the capture: only max_s may cut it off"
    assert vad.started is False
    assert vad.finished is True
    assert result.pcm is None
    assert result.reason == "silence"
    assert result.speech_s == 0.0
    assert result.total_s == pytest.approx(0.8)


def test_vad_collects_speech_and_frames_the_utterance():
    """2. Speech right after calibration is returned as one utterance."""
    vad = EnergyVad(config())
    taken = consume(vad, silence(10) + speech(10))
    result = vad.finish()

    assert vad.started is True
    assert taken == 20, "the stream ended by itself; nothing else stopped it"
    assert result.reason == "ok"
    assert result.pcm is not None
    assert len(result.pcm) == 10 * BLOCK_BYTES
    assert result.speech_s == pytest.approx(0.2)
    assert result.total_s == pytest.approx(0.4)
    # a silent room: the absolute floor (not the measured one) decides
    assert result.noise_floor == pytest.approx(0.0)
    assert result.threshold == pytest.approx(0.012)


def test_vad_stops_after_the_configured_silence():
    """3. Speech then a pause: the pause ends the utterance after exactly silence_s."""
    vad = EnergyVad(config())
    taken = consume(vad, silence(10) + speech(10) + silence(30))
    result = vad.finish()

    assert taken == 30, "10 calibration + 10 speech + 10 silent blocks end the utterance"
    assert result.reason == "ok"
    assert result.pcm is not None
    assert len(result.pcm) == 20 * BLOCK_BYTES, "the trailing quiet is part of the recording"
    assert result.speech_s == pytest.approx(0.2)


def test_vad_caps_a_never_silent_utterance():
    """4. Continuous speech is truncated at max_s instead of growing forever."""
    vad = EnergyVad(config())
    taken = consume(vad, silence(10) + speech(60))
    result = vad.finish()

    assert taken == 50, "1.0 s of audio at 20 ms per block"
    assert result.reason == "max"
    assert result.pcm is not None
    assert len(result.pcm) == 40 * BLOCK_BYTES, "calibration is not part of the utterance"
    assert result.total_s == pytest.approx(1.0)


def test_vad_ignores_room_noise_below_the_absolute_floor():
    """5. A hissing room must not be mistaken for speech."""
    result = detect_utterance(room_noise(30), config())

    assert result.pcm is None
    assert result.reason == "silence"
    assert result.noise_floor < 0.012, "the synthetic noise really is below the floor"


def test_vad_calibrates_a_noisy_room_and_scales_the_threshold():
    """5b. On a loud room the measured floor (times the multiplier) has to win."""
    loud = room_noise(10, amplitude=800)
    vad = EnergyVad(config())
    consume(vad, loud)
    quiet_after = config()
    assert vad.noise_floor > quiet_after.floor, "800 amplitude is above the absolute floor"
    assert vad.threshold == pytest.approx(vad.noise_floor * 3.0)
    assert vad.threshold > quiet_after.floor

    # the same noise is now below the threshold: no utterance
    assert consume(vad, room_noise(20, amplitude=800)) == 20
    assert vad.finish().pcm is None


def test_vad_rejects_a_too_short_sound():
    """6. A door slam is not an utterance."""
    result = detect_utterance(silence(10) + speech(2) + silence(20), config(min_speech_s=0.3))

    assert result.pcm is None
    assert result.reason == "too-short"


def test_vad_keeps_a_little_audio_from_before_the_speech():
    """7. The pre-roll keeps the first syllable from being clipped."""
    vad = EnergyVad(config(preroll_s=0.04))  # 2 blocks
    consume(vad, silence(10) + room_noise(2, amplitude=20) + speech(1))
    result = vad.finish()

    assert result.pcm is not None
    assert len(result.pcm) == 3 * BLOCK_BYTES, "2 pre-roll blocks + the speech block"
    assert result.speech_s == pytest.approx(0.02), "only the loud block counts as speech"


def test_vad_reports_the_reason_in_chinese():
    assert "静音" in silence_message("silence")
    assert "很短" in silence_message("too-short")
    assert silence_message("") == VAD_REASONS["silence"]
    assert silence_message("nonsense") == VAD_REASONS["silence"]


def test_rms_int16_measures_amplitude():
    assert rms_int16(b"") == 0.0
    assert rms_int16(b"\x00\x00\x00") == 0.0, "a half sample is ignored"
    assert rms_int16(b"\x00\x00" * 100) == 0.0
    assert rms_int16(tone(400, amplitude=0.5)) == pytest.approx(0.5 * 0.7071, rel=0.02)


# ----------------------------------------------------------------------------- WAV


def test_pack_wav_writes_a_canonical_16khz_mono_header():
    pcm = tone(1600)
    data = pack_wav(pcm)

    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"
    assert data[12:16] == b"fmt "
    assert struct.unpack("<I", data[4:8])[0] == len(data) - 8, "RIFF size counts the payload"
    assert struct.unpack("<I", data[16:20])[0] == 16, "PCM fmt chunk"
    assert struct.unpack("<HHIIHH", data[20:36]) == (
        1,  # PCM
        1,  # mono
        RATE,  # 16 kHz
        RATE * 2,  # byte rate
        2,  # block align
        16,  # bits per sample
    )
    assert data[36:40] == b"data"
    assert struct.unpack("<I", data[40:44])[0] == len(pcm), "payload length is the sample count"
    assert data[44:] == pcm


def test_resampling_a_device_rate_to_16khz():
    pcm = tone(4800, frequency=440)  # 0.1 s at 48 kHz
    resampled = resample_int16(pcm, 48000, RATE)

    assert len(resampled) == 1600 * 2
    assert resample_int16(pcm, 48000, 48000) is pcm, "no work when the rate already matches"
    assert resample_int16(b"", 48000, RATE) == b""
    # the pure-Python fallback (used when numpy is missing) must agree on the length
    assert len(_linear_resample(pcm, 48000, RATE)) == len(resampled)


# ----------------------------------------------------------------------------- TTS


def test_build_speak_command_contains_the_escaped_text():
    command = build_speak_command("你好，it's fine", max_chars=100)

    assert command[0] == "powershell"
    assert "-NoProfile" in command and "-NonInteractive" in command
    script = command[-1]
    assert "System.Speech" in script, "SAPI is what makes this work without new dependencies"
    assert "SpeechSynthesizer" in script
    assert "你好，it''s fine" in script, "a single quote must be doubled for PowerShell"
    assert "it's fine" not in script


def test_build_speak_command_truncates_to_the_limit():
    text = "字" * 500

    assert truncate_for_speech(text, 300) == "字" * 299 + "…"
    assert len(truncate_for_speech(text, 300)) == 300
    assert truncate_for_speech(text, 1) == "…"
    assert truncate_for_speech(text, 0) == ""
    assert truncate_for_speech("短", 10) == "短", "nothing is added when it already fits"
    script = build_speak_command(text, max_chars=300)[-1]
    assert "字" * 299 + "…" in script
    assert "字" * 300 not in script


def test_truncate_for_speech_flattens_the_answer():
    assert flatten_for_speech("第一行\n\n`代码` *强调*") == "第一行 代码 强调"
    assert truncate_for_speech("a\n b", 10) == "a b"


def test_speak_is_silent_when_tts_is_off(monkeypatch):
    monkeypatch.setattr(settings, "voice_tts", False)
    console = Console(record=True, width=110, no_color=True, force_terminal=False)
    called: list[list[str]] = []

    def runner(command: list[str]) -> bool:  # pragma: no cover - must never run
        called.append(command)
        raise AssertionError("PowerShell must not run while /speak is off")

    assert speak("你好", console=console, runner=runner) is False
    assert called == []
    assert "朗读已关闭" in console.export_text()


def test_speak_with_nothing_to_say_returns_false():
    console = Console(record=True, width=110, no_color=True, force_terminal=False)

    def runner(command: list[str]) -> bool:  # pragma: no cover - must never run
        raise AssertionError("PowerShell must not run for empty text")

    assert speak("   \n\t ", console=console, runner=runner) is False
    assert "没有可朗读的文本" in console.export_text()


def test_speak_runs_the_sapi_command(monkeypatch):
    monkeypatch.setattr(settings, "voice_tts", True)
    monkeypatch.setattr(settings, "voice_tts_max_chars", 50)
    monkeypatch.setattr(voice_module, "powershell_executable", lambda: r"C:\Windows\powershell.exe")
    seen: list[list[str]] = []

    assert speak("你好世界", runner=lambda command: seen.append(command) or True) is True
    assert len(seen) == 1
    assert seen[0][0] == r"C:\Windows\powershell.exe"
    assert "你好世界" in seen[0][-1]


def test_speak_reports_a_missing_powershell(monkeypatch):
    monkeypatch.setattr(settings, "voice_tts", True)
    monkeypatch.setattr(voice_module, "powershell_executable", lambda: None)
    console = Console(record=True, width=110, no_color=True, force_terminal=False)

    def runner(command: list[str]) -> bool:  # pragma: no cover - must never run
        raise AssertionError("nothing to run without powershell.exe")

    assert speak("你好", console=console, runner=runner) is False
    assert "powershell" in console.export_text()


def test_speak_says_when_the_synthesiser_failed(monkeypatch):
    monkeypatch.setattr(settings, "voice_tts", True)
    monkeypatch.setattr(voice_module, "powershell_executable", lambda: "powershell")
    console = Console(record=True, width=110, no_color=True, force_terminal=False)

    assert speak("你好", console=console, runner=lambda command: False) is False
    assert "朗读失败" in console.export_text()


def test_speak_notes_a_truncation(monkeypatch):
    monkeypatch.setattr(settings, "voice_tts", True)
    monkeypatch.setattr(settings, "voice_tts_max_chars", 5)
    monkeypatch.setattr(voice_module, "powershell_executable", lambda: "powershell")
    console = Console(record=True, width=110, no_color=True, force_terminal=False)

    assert speak("一二三四五六七八九十", console=console, runner=lambda command: True) is True
    assert "已截断到 5 字" in console.export_text()


class _Completed:
    """Enough of subprocess.CompletedProcess for the runner."""

    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


def test_run_command_maps_failures_to_false(monkeypatch):
    monkeypatch.setattr(voice_module.subprocess, "run", lambda *a, **k: _Completed(1, "boom"))
    assert voice_module._run_command(["powershell"]) is False

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise OSError("powershell.exe is gone")

    monkeypatch.setattr(voice_module.subprocess, "run", boom)
    assert voice_module._run_command(["powershell"]) is False


def test_run_command_decodes_powershell_output_leniently(monkeypatch):
    """PowerShell writes in the console code page: strict decoding must not break TTS."""
    seen: dict = {}

    def fake(command, **kwargs):  # noqa: ANN001
        seen.update(kwargs)
        return _Completed(0)

    monkeypatch.setattr(voice_module.subprocess, "run", fake)
    assert voice_module._run_command(["powershell"]) is True
    assert seen["errors"] == "replace"


# ---------------------------------------------------------------------------- ASR


class FakeResponse:
    """Enough of an httpx response for one stubbed /asr call."""

    def __init__(self, status_code: int = 200, body: object | None = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def test_transcribe_posts_the_audio_with_the_control_token(monkeypatch):
    seen: dict = {}

    def fake_post(url, content=None, headers=None, params=None, timeout=None):  # noqa: ANN001
        seen.update(url=url, content=content, headers=headers, params=params, timeout=timeout)
        return FakeResponse(200, {"ok": True, "text": "你好", "language": "zh", "duration_s": 1.0, "model": "small"})

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr(settings, "asr_language", "zh")
    body = voice_module.transcribe(b"RIFF....WAVE-fake", media_type="audio/wav")

    assert body["text"] == "你好"
    assert seen["url"].endswith("/asr")
    assert seen["content"] == b"RIFF....WAVE-fake"
    assert seen["headers"]["Content-Type"] == "audio/wav"
    assert seen["headers"]["X-Agent-Token"] == settings.control_secret
    assert seen["params"] == {"language": "zh"}
    assert seen["timeout"] >= 60.0, "a small CPU model can take a while on a long clip"


def test_transcribe_language_hint_follows_the_setting(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        "httpx.post",
        lambda url, **kwargs: seen.update(kwargs) or FakeResponse(200, {"ok": True, "text": "x"}),
    )
    monkeypatch.setattr(settings, "asr_language", "")
    voice_module.transcribe(b"audio")
    assert seen["params"] == {"language": "zh"}, "the console speaks Chinese by default"

    monkeypatch.setattr(settings, "asr_language", "en")
    voice_module.transcribe(b"audio")
    assert seen["params"] == {"language": "en"}, "AGENT_ASR_LANGUAGE wins when it is set"

    voice_module.transcribe(b"audio", language="")
    assert seen["params"] is None, "an explicitly empty hint means auto-detect"


def test_transcribe_rejects_empty_audio_before_any_request(monkeypatch):
    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise AssertionError("empty audio must not reach the service")

    monkeypatch.setattr("httpx.post", boom)
    with pytest.raises(RuntimeError, match="音频是空的"):
        voice_module.transcribe(b"")


def test_transcribe_reports_an_unreachable_service(monkeypatch):
    import httpx

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.post", boom)
    with pytest.raises(RuntimeError, match="无法连接 AI 服务（语音识别）"):
        voice_module.transcribe(b"audio")


def test_transcribe_maps_service_errors(monkeypatch):
    monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResponse(503, {"ok": False, "error": "ASR is disabled"}))
    with pytest.raises(RuntimeError, match="AGENT_ASR_ENABLED"):
        voice_module.transcribe(b"audio")

    monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResponse(401, {"ok": False, "error": "nope"}))
    with pytest.raises(RuntimeError, match="AGENT_CONTROL_SECRET"):
        voice_module.transcribe(b"audio")

    monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResponse(500, None))
    with pytest.raises(RuntimeError, match="无法解析的响应"):
        voice_module.transcribe(b"audio")

    monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResponse(500, {"ok": False, "error": "boom"}))
    with pytest.raises(RuntimeError, match="语音识别失败：boom"):
        voice_module.transcribe(b"audio")


# ---------------------------------------------------------------------------- loop


class FakePrompt:
    """The operator: a scripted list of lines, remembering the prompts it was shown."""

    def __init__(self, *lines: str) -> None:
        self.lines = list(lines)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.lines.pop(0) if self.lines else "q"


class FakeRecorder:
    """Stands in for the microphone: prepared WAV buffers, then permanent silence."""

    def __init__(self, chunks: list[bytes | None], reason: str = "silence") -> None:
        self.chunks = list(chunks)
        self.calls = 0
        self.last_reason = reason

    def record_utterance(self) -> bytes | None:
        self.calls += 1
        return self.chunks.pop(0) if self.chunks else None


def transcriber(text: str, seen: dict | None = None):
    """A stub /asr: same shape as the service, records what it was handed."""

    def fake(wav: bytes, **kwargs) -> dict:
        if seen is not None:
            seen["wav"] = wav
        return {"ok": True, "text": text, "language": "zh", "duration_s": 1.0, "model": "small"}

    return fake


def quiet_console() -> Console:
    return Console(record=True, width=110, no_color=True, force_terminal=False)


def test_voice_loop_hands_the_transcript_over_and_speaks_the_answer(monkeypatch):
    monkeypatch.setattr(settings, "voice_tts", True)
    console = quiet_console()
    prompt = FakePrompt("", "q")  # Enter to speak, then q to leave
    wav = pack_wav(tone(RATE // 2))
    recorder = FakeRecorder([wav])
    seen: dict = {}
    spoken: list[str] = []

    def answer(text: str) -> str:
        seen["text"] = text
        return "这是回答"

    mode = voice_loop(
        console=console,
        ask=prompt,
        answer=answer,
        recorder=recorder,
        transcribe_fn=transcriber("你好，世界", seen),
        speak_fn=lambda text: spoken.append(text) or True,
    )
    out = console.export_text()

    assert mode == "off"
    assert recorder.calls == 1
    assert seen["wav"] == wav, "exactly what the recorder produced goes to /asr"
    assert seen["text"] == "你好，世界", "the transcript reaches the caller (the REPL)"
    assert spoken == ["这是回答"], "TTS receives the answer, not the transcript"
    assert "识别中…" in out
    assert "识别到：你好，世界" in out
    assert CONTINUE_PROMPT in prompt.prompts[0], "push-to-talk asks before each utterance"
    assert "已退出语音模式" in out


def test_voice_loop_q_exits_without_touching_the_microphone(monkeypatch):
    monkeypatch.setattr(settings, "voice_tts", True)
    console = quiet_console()
    recorder = FakeRecorder([pack_wav(tone(100))])
    answered: list[str] = []
    transcribed: list[bytes] = []

    mode = voice_loop(
        console=console,
        ask=FakePrompt("q"),
        answer=lambda text: answered.append(text),
        recorder=recorder,
        transcribe_fn=lambda wav, **kwargs: transcribed.append(wav) or {"ok": True, "text": "x"},
        speak_fn=lambda text: True,
    )

    assert mode == "off"
    assert recorder.calls == 0 and answered == [] and transcribed == []


@pytest.mark.parametrize("word", ["q", "Q", "quit", "exit", "/voice-mode off"])
def test_voice_loop_stop_words_all_leave_the_mode(monkeypatch, word):
    monkeypatch.setattr(settings, "voice_tts", True)
    assert voice_loop(
        console=quiet_console(),
        ask=FakePrompt(word),
        answer=lambda text: None,
        recorder=FakeRecorder([]),
        transcribe_fn=transcriber("x"),
        speak_fn=lambda text: True,
    ) == "off"


def test_voice_loop_ctrl_c_at_the_prompt_leaves_the_mode(monkeypatch):
    """Ctrl-C *at the prompt* still ends voice mode (the REPL catches it and exits)."""
    monkeypatch.setattr(settings, "voice_tts", True)

    def ask(prompt: str) -> str:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        voice_loop(
            console=quiet_console(),
            ask=ask,
            answer=lambda text: None,
            recorder=FakeRecorder([]),
            transcribe_fn=transcriber("x"),
            speak_fn=lambda text: True,
        )


def test_voice_loop_end_of_input_leaves_the_mode(monkeypatch):
    """EOF at the prompt propagates: main.cmd_chat treats it as "leave the console"."""
    monkeypatch.setattr(settings, "voice_tts", True)

    def ask(prompt: str) -> str:
        raise EOFError

    with pytest.raises(EOFError):
        voice_loop(
            console=quiet_console(),
            ask=ask,
            answer=lambda text: None,
            recorder=FakeRecorder([]),
            transcribe_fn=transcriber("x"),
            speak_fn=lambda text: True,
        )


def test_voice_loop_accepts_typed_text_instead_of_a_recording(monkeypatch):
    """Voice mode must not trap the operator into a microphone-only interface."""
    monkeypatch.setattr(settings, "voice_tts", True)
    console = quiet_console()
    recorder = FakeRecorder([pack_wav(tone(100))])
    answered: list[str] = []

    voice_loop(
        console=console,
        ask=FakePrompt("直接输入的问题", "q"),
        answer=lambda text: answered.append(text),
        recorder=recorder,
        transcribe_fn=transcriber("x"),
        speak_fn=lambda text: True,
    )

    assert answered == ["直接输入的问题"]
    assert recorder.calls == 0


def test_voice_loop_refuses_other_commands_without_crashing(monkeypatch):
    monkeypatch.setattr(settings, "voice_tts", True)
    console = quiet_console()
    answered: list[str] = []

    voice_loop(
        console=console,
        ask=FakePrompt("/net on", "q"),
        answer=lambda text: answered.append(text),
        recorder=FakeRecorder([]),
        transcribe_fn=transcriber("x"),
        speak_fn=lambda text: True,
    )

    assert answered == []
    assert "其他命令请先用 q 退出语音模式" in console.export_text()


def test_voice_loop_says_when_it_heard_nothing(monkeypatch):
    monkeypatch.setattr(settings, "voice_tts", True)
    console = quiet_console()
    transcribed: list[bytes] = []
    recorder = FakeRecorder([None])

    voice_loop(
        console=console,
        ask=FakePrompt("", "q"),
        answer=lambda text: None,
        recorder=recorder,
        transcribe_fn=lambda wav, **kwargs: transcribed.append(wav) or {"ok": True, "text": "x"},
        speak_fn=lambda text: True,
    )

    assert transcribed == [], "silence must never be uploaded"
    assert "没有听到声音" in console.export_text()


def test_voice_loop_uses_the_vad_reason_for_the_message(monkeypatch):
    monkeypatch.setattr(settings, "voice_tts", True)
    console = quiet_console()

    voice_loop(
        console=console,
        ask=FakePrompt("", "q"),
        answer=lambda text: None,
        recorder=FakeRecorder([None], reason="too-short"),
        transcribe_fn=transcriber("x"),
        speak_fn=lambda text: True,
    )

    assert VAD_REASONS["too-short"] in console.export_text()


def test_voice_loop_degrades_when_the_service_is_down(monkeypatch):
    """An unreachable /asr must be a Chinese line, not a traceback."""
    monkeypatch.setattr(settings, "voice_tts", True)
    console = quiet_console()

    def boom(wav: bytes, **kwargs) -> dict:
        raise RuntimeError("无法连接 AI 服务（语音识别）：connection refused")

    mode = voice_loop(
        console=console,
        ask=FakePrompt("", "q"),
        answer=lambda text: None,
        recorder=FakeRecorder([pack_wav(tone(100))]),
        transcribe_fn=boom,
        speak_fn=lambda text: True,
    )

    assert mode == "off", "the loop keeps running until the operator stops it"
    assert "无法连接 AI 服务（语音识别）" in console.export_text()


def test_voice_loop_ignores_an_empty_transcript(monkeypatch):
    monkeypatch.setattr(settings, "voice_tts", True)
    console = quiet_console()
    answered: list[str] = []

    voice_loop(
        console=console,
        ask=FakePrompt("", "q"),
        answer=lambda text: answered.append(text),
        recorder=FakeRecorder([pack_wav(tone(100))]),
        transcribe_fn=transcriber("   "),
        speak_fn=lambda text: True,
    )

    assert answered == []
    assert "没有识别到语音内容" in console.export_text()


def test_voice_loop_does_not_speak_when_tts_is_off(monkeypatch):
    monkeypatch.setattr(settings, "voice_tts", False)
    console = quiet_console()
    answered: list[str] = []

    def speaker(text: str) -> bool:  # pragma: no cover - must never run
        raise AssertionError("/speak off must silence the loop")

    voice_loop(
        console=console,
        ask=FakePrompt("", "q"),
        answer=lambda text: answered.append(text) or "回答",
        recorder=FakeRecorder([pack_wav(tone(100))]),
        transcribe_fn=transcriber("你好"),
        speak_fn=speaker,
    )

    assert answered == ["你好"]


def test_voice_loop_uses_the_module_transcriber_and_speaker(monkeypatch):
    """The defaults are the real functions, resolved late: monkeypatching them is enough.

    The answer is spoken through ``say_with_barge_in`` (the interruptible speaker), not
    through the blocking ``speak`` -- that is the whole point of barge-in.
    """
    monkeypatch.setattr(settings, "voice_tts", True)
    monkeypatch.setattr(voice_module, "transcribe", transcriber("模块识别"))
    spoken: list[str] = []
    monkeypatch.setattr(
        voice_module, "say_with_barge_in", lambda text, console=None: spoken.append(text) or False
    )
    answered: list[str] = []

    voice_loop(
        console=quiet_console(),
        ask=FakePrompt("", "q"),
        answer=lambda text: answered.append(text) or "回答内容",
        recorder=FakeRecorder([pack_wav(tone(100))]),
    )

    assert answered == ["模块识别"], "the default transcriber is voice.transcribe, looked up late"
    assert spoken == ["回答内容"], "the default speaker is voice.say_with_barge_in, looked up late"


def test_voice_loop_keeps_going_when_speech_is_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "voice_tts", True)
    answered: list[str] = []

    voice_loop(
        console=quiet_console(),
        ask=FakePrompt("", "", "q"),
        answer=lambda text: answered.append(text) or "回答",
        recorder=FakeRecorder([pack_wav(tone(100)), pack_wav(tone(100))]),
        transcribe_fn=transcriber("你好"),
        speak_fn=lambda text: False,  # SAPI unusable: the answer is still printed
    )

    assert answered == ["你好", "你好"]


class FakeKeyboard:
    """A scripted console keyboard: answers any_key_pressed() from a list of bools."""

    def __init__(self, presses: int = 0) -> None:
        self.presses = presses
        self.calls = 0
        self.discarded = 0

    def any_key_pressed(self) -> bool:
        self.calls += 1
        return self.calls > self.presses

    def discard(self) -> None:
        self.discarded += 1


class FakeSpeechProcess:
    """A SAPI process that only ever finishes when it is terminated (like a long answer).

    ``finish_after`` makes it exit by itself after that many ``poll()`` calls, which is what
    a short answer does; the default (``None``) never finishes, which is what a long one
    does until a keypress or the timeout kills it.
    """

    def __init__(self, code: int | None = None, *, finish_after: int | None = None) -> None:
        self.code = code
        self.finish_after = finish_after
        self.polls = 0
        self.terminated = 0
        self.waits = 0

    def poll(self) -> int | None:
        if self.terminated:
            return 1
        self.polls += 1
        if self.finish_after is not None and self.polls >= self.finish_after:
            return 0 if self.code is None else self.code
        return self.code

    def terminate(self) -> None:
        self.terminated += 1

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 - mirrors Popen
        self.waits += 1
        return 1


def test_speech_handle_polls_until_the_utterance_ends(monkeypatch):
    # no key is ever pressed: this utterance ends by itself, so nothing may kill it
    monkeypatch.setattr(voice_module, "any_key_pressed", lambda: False)
    process = FakeSpeechProcess(finish_after=3)
    handle = SpeechHandle(process)

    assert handle.poll() is None, "still speaking"
    assert handle.wait(poll_s=0.001, timeout=0.05) is False, "it ended on its own"
    assert process.terminated == 0, "a finished utterance is never killed"
    assert handle.poll() is False, "and the handle stays finished"


def test_speech_handle_stops_the_process_and_reports_the_interruption(monkeypatch):
    keyboard = FakeKeyboard(presses=0)  # the first poll already sees a key
    monkeypatch.setattr(voice_module, "keyboard", keyboard)
    process = FakeSpeechProcess()
    handle = SpeechHandle(process)

    assert handle.wait(poll_s=0.001) is True, "a keypress must stop the speech"
    assert process.terminated == 1, "the SAPI process must actually be stopped"


def test_speak_with_barge_in_stops_when_a_key_is_pressed(monkeypatch):
    """(c) the proof without a speaker: a keypress stops the fake TTS and is asserted."""
    monkeypatch.setattr(settings, "voice_tts", True)
    monkeypatch.setattr(voice_module, "powershell_executable", lambda: "powershell")
    monkeypatch.setattr(voice_module, "keyboard", FakeKeyboard(presses=2))
    console = quiet_console()
    process = FakeSpeechProcess()
    commands: list[list[str]] = []

    interrupted = say_with_barge_in(
        "这是一段很长的回答", console=console, runner=lambda command: commands.append(command) or SpeechHandle(process)
    )

    assert interrupted is True
    assert process.terminated == 1, "the utterance was stopped, not waited out"
    assert SPEAKING_MESSAGE in output(console), "the state line says a key can interrupt"
    assert "已打断朗读" in output(console)
    assert len(commands) == 1 and "System.Speech" in commands[0][-1]


def test_speak_with_barge_in_returns_false_when_it_finishes(monkeypatch):
    monkeypatch.setattr(settings, "voice_tts", True)
    monkeypatch.setattr(voice_module, "powershell_executable", lambda: "powershell")
    monkeypatch.setattr(voice_module, "keyboard", FakeKeyboard(presses=0))
    process = FakeSpeechProcess(code=0)  # reads the whole answer and exits

    assert say_with_barge_in("短", runner=lambda command: SpeechHandle(process)) is False
    assert process.terminated == 0


def test_speak_with_barge_in_never_runs_powershell_when_tts_is_off(monkeypatch):
    monkeypatch.setattr(settings, "voice_tts", False)

    def runner(command: list[str]) -> bool:  # pragma: no cover - must never run
        raise AssertionError("/speak off must silence the loop")

    assert say_with_barge_in("你好", runner=runner) is False


def test_voice_loop_space_is_text_and_never_starts_a_recording(monkeypatch):
    """(a) a lone space must not record and must not become a message either.

    It is *text with nothing in it*: the plain prompt drops it (main.cmd_chat strips the
    line and continues on empty), /help voice-mode promises "只按一下空格不会开始录音、
    也不会发送", and only a genuinely empty line means "talk".
    """
    monkeypatch.setattr(settings, "voice_tts", True)
    console = quiet_console()
    recorder = FakeRecorder([pack_wav(tone(100))])
    answered: list[str] = []

    voice_loop(
        console=console,
        ask=FakePrompt(" ", "q"),
        answer=lambda text: answered.append(text) or "回答",
        recorder=recorder,
        transcribe_fn=transcriber("x"),
        speak_fn=lambda text: True,
    )

    assert recorder.calls == 0, "a lone space is text, not a trigger"
    assert answered == [], "whitespace-only text carries no message to send"
    assert "已退出语音模式" in output(console), "the loop stayed alive and took the next line"


def test_voice_loop_enter_on_an_empty_line_records_one_utterance(monkeypatch):
    """(b) bare Enter is the documented default: one recording, synthetic audio."""
    monkeypatch.setattr(settings, "voice_tts", False)
    console = quiet_console()
    wav = pack_wav(tone(RATE // 2))
    recorder = FakeRecorder([wav])
    answered: list[str] = []

    voice_loop(
        console=console,
        ask=FakePrompt("", "q"),
        answer=lambda text: answered.append(text) or "回答",
        recorder=recorder,
        transcribe_fn=transcriber("按回车说话了"),
        speak_fn=lambda text: True,
    )

    assert recorder.calls == 1
    assert answered == ["按回车说话了"]
    assert RECORDING_MESSAGE in output(console), "the prompt says exactly what is happening"
    assert TRANSCRIBING_MESSAGE in output(console)


def test_voice_loop_talk_key_records_while_a_line_is_being_typed(monkeypatch):
    """The explicit binding (Ctrl+T) is a sentinel, so it can never be confused with text."""
    monkeypatch.setattr(settings, "voice_tts", False)
    assert isinstance(TALK_SENTINEL, TalkKey), "the loop keys off isinstance, not a magic string"
    console = quiet_console()
    wav = pack_wav(tone(RATE // 2))
    recorder = FakeRecorder([wav])
    answered: list[str] = []

    voice_loop(
        console=console,
        ask=FakePrompt(TALK_SENTINEL, "q"),
        answer=lambda text: answered.append(text) or "回答",
        recorder=recorder,
        transcribe_fn=transcriber("用快捷键说话"),
        speak_fn=lambda text: True,
    )

    assert recorder.calls == 1
    assert answered == ["用快捷键说话"]


def test_voice_loop_prompt_lines_state_every_phase(monkeypatch):
    """The three states the operator asked for, verbatim."""
    monkeypatch.setattr(settings, "voice_tts", True)
    console = quiet_console()
    spoken: list[str] = []

    voice_loop(
        console=console,
        ask=FakePrompt("", "q"),
        answer=lambda text: "回答内容",
        recorder=FakeRecorder([pack_wav(tone(100))]),
        transcribe_fn=transcriber("你好"),
        speak_fn=lambda text: spoken.append(text) or True,
    )
    out = output(console)

    assert "按回车或 Ctrl+T 说话 · 输入 q 退出" in out, "the binding is printed in the prompt"
    assert CONTINUE_PROMPT in out, "the prompt the loop prints is the shared constant"
    assert TALK_KEY in out, "the prompt names the dedicated talk key, not just Enter"
    assert "🎤 录音中…（停顿 1.2 秒自动结束 / 按 Ctrl+C 取消）" in out
    assert "识别中…" in out
    assert spoken == ["回答内容"]


def test_voice_loop_speaks_the_answer_through_the_interruptible_speaker(monkeypatch):
    """The default speaker is say_with_barge_in, so a keypress ends the read-out."""
    monkeypatch.setattr(settings, "voice_tts", True)
    monkeypatch.setattr(voice_module, "powershell_executable", lambda: "powershell")
    keyboard = FakeKeyboard(presses=1)
    monkeypatch.setattr(voice_module, "keyboard", keyboard)
    console = quiet_console()
    process = FakeSpeechProcess()
    monkeypatch.setattr(
        voice_module, "start_speech", lambda command: SpeechHandle(process)
    )

    voice_loop(
        console=console,
        ask=FakePrompt("", "q"),
        answer=lambda text: "很长的回答，需要被打断",
        recorder=FakeRecorder([pack_wav(tone(100))]),
        transcribe_fn=transcriber("你好"),
    )
    out = output(console)

    assert "🔊 朗读中（按任意键打断）" in out
    assert process.terminated == 1, "barge-in reached the fake SAPI process"
    assert keyboard.discarded == 1, "the interrupting keypress is dropped, not re-read"
    assert "已打断朗读" in out


def test_voice_loop_cancelled_turn_keeps_the_session_alive(monkeypatch):
    """(d) Ctrl-C while the agent is generating cancels the turn, not the console."""
    monkeypatch.setattr(settings, "voice_tts", False)
    console = quiet_console()
    calls = {"n": 0}

    def answer(text: str) -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt  # exactly what _stream_turn lets through
        return "第二轮的回答"

    mode = voice_loop(
        console=console,
        ask=FakePrompt("", "", "q"),
        answer=answer,
        recorder=FakeRecorder([pack_wav(tone(100)), pack_wav(tone(100))]),
        transcribe_fn=transcriber("你好"),
        speak_fn=lambda text: True,
    )
    out = output(console)

    assert mode == "off", "the loop only ends when the operator says so"
    assert calls["n"] == 2, "the session survived the cancelled turn and kept going"
    assert "本轮已取消" in out
    assert "已退出语音模式" in out


def test_voice_loop_tolerates_a_recorder_that_returns_a_bare_keypress(monkeypatch):
    """A reader may hand back the sentinel object itself: the loop must not .strip() it."""
    monkeypatch.setattr(settings, "voice_tts", False)
    console = quiet_console()

    mode = voice_loop(
        console=console,
        ask=FakePrompt(TALK_SENTINEL, "q"),
        answer=lambda text: None,
        recorder=FakeRecorder([None]),
        transcribe_fn=transcriber("x"),
        speak_fn=lambda text: True,
    )

    assert mode == "off"
    assert "没有听到声音" in output(console), "the sentinel started a recording, as intended"


class FakeStream:
    """The parts of a sounddevice input stream the recorder drives: open, read, close."""

    def __init__(self, blocks: list[bytes], error: Exception | None = None) -> None:
        self.blocks = list(blocks)
        self.error = error
        self.read_sizes: list[int] = []

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self, samples: int) -> tuple[bytes, bool]:
        self.read_sizes.append(samples)
        if self.error is not None:
            raise self.error
        if not self.blocks:
            raise AssertionError("the recorder asked for more audio than the script provided")
        return self.blocks.pop(0), False


class FakeSoundDevice:
    """The parts of the sounddevice module :class:`SoundDeviceRecorder` uses."""

    def __init__(self, stream: FakeStream, *, accepts_16k: bool = True, device_rate: int = RATE) -> None:
        self.stream = stream
        self.accepts_16k = accepts_16k
        self.device_rate = device_rate
        self.opened: list[dict] = []
        self.checked: list[dict] = []

    def check_input_settings(self, **kwargs) -> None:  # noqa: ANN003
        self.checked.append(kwargs)
        if not self.accepts_16k:
            raise RuntimeError("Invalid sample rate [PaErrorCode -9997]")

    def query_devices(self, device=None):  # noqa: ANN001, ARG002
        return {"name": "fake", "default_samplerate": float(self.device_rate), "max_input_channels": 2}

    def RawInputStream(self, **kwargs) -> FakeStream:  # noqa: N802 - mirrors the sounddevice API
        self.opened.append(kwargs)
        return self.stream


def install_recorder(monkeypatch, sd: FakeSoundDevice, **kwargs) -> voice_module.SoundDeviceRecorder:
    monkeypatch.setattr(voice_module, "_import_sounddevice", lambda: sd)
    return voice_module.SoundDeviceRecorder(**kwargs)


def block(samples: int, *, loud: bool) -> bytes:
    return tone(samples, amplitude=0.5) if loud else b"\x00\x00" * samples


def parse_wav(data: bytes) -> dict[str, int]:
    """Read the header back by hand (a wave-module round trip would prove less)."""
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    assert data[12:16] == b"fmt " and data[36:40] == b"data"
    audio_format, channels, rate, byte_rate, block_align, bits = struct.unpack("<HHIIHH", data[20:36])
    assert audio_format == 1, "PCM"
    assert byte_rate == rate * channels * bits // 8 and block_align == channels * bits // 8
    return {
        "channels": channels,
        "rate": rate,
        "width": bits // 8,
        "payload": struct.unpack("<I", data[40:44])[0],
    }


def test_recorder_drives_the_vad_from_a_fake_microphone(monkeypatch):
    """The real recorder code (open -> read -> VAD -> WAV) without any hardware."""
    samples = RATE * voice_module.BLOCK_MS // 1000  # 30 ms at 16 kHz
    script = [block(samples, loud=False)] * 17 + [block(samples, loud=True)] * 10
    script += [block(samples, loud=False)] * 40
    sd = FakeSoundDevice(FakeStream(script))
    recorder = install_recorder(monkeypatch, sd, silence_s=0.2, max_s=1.0)

    wav = recorder.record_utterance()
    assert wav is not None
    assert sd.checked[0]["samplerate"] == RATE, "16 kHz is asked for first"
    assert sd.opened == [
        {"samplerate": RATE, "channels": 1, "dtype": "int16", "blocksize": samples, "device": None}
    ]
    # 0.5 s calibration, then the utterance: 6 pre-roll + 10 speech + 7 trailing blocks
    assert parse_wav(wav) == {"channels": 1, "rate": RATE, "width": 2, "payload": 23 * samples * 2}
    assert recorder.last_reason == "ok"


def test_recorder_uses_the_device_rate_when_16khz_is_refused(monkeypatch):
    """WASAPI/WDM-KS refuse 16 kHz: capture at the device rate, resample, keep the length."""
    rate = 48_000
    samples = rate * voice_module.BLOCK_MS // 1000
    script = [block(samples, loud=False)] * 17 + [block(samples, loud=True)] * 10
    script += [block(samples, loud=False)] * 40
    sd = FakeSoundDevice(FakeStream(script), accepts_16k=False, device_rate=rate)
    recorder = install_recorder(monkeypatch, sd, silence_s=0.2, max_s=1.0)

    wav = recorder.record_utterance()
    assert wav is not None
    assert sd.opened[0]["samplerate"] == rate and sd.opened[0]["blocksize"] == samples
    header = parse_wav(wav)
    assert header["rate"] == RATE, "the /asr request always carries 16 kHz"
    # the same 23 blocks, resampled from 48 kHz: 23 * 1440 samples / 3
    assert header["payload"] == 23 * samples * 2 // 3
    assert recorder.last_reason == "ok"


def test_recorder_returns_nothing_for_silence_only(monkeypatch):
    samples = RATE * voice_module.BLOCK_MS // 1000
    sd = FakeSoundDevice(FakeStream([block(samples, loud=False)] * 60))
    recorder = install_recorder(monkeypatch, sd, max_s=1.0)

    assert recorder.record_utterance() is None
    assert recorder.last_reason == "timeout", "nothing was heard before the cap"


def test_recorder_wraps_a_device_failure_in_chinese(monkeypatch):
    sd = FakeSoundDevice(FakeStream([], error=OSError("device lost")))
    recorder = install_recorder(monkeypatch, sd)

    with pytest.raises(VoiceError) as excinfo:
        recorder.record_utterance()
    message = str(excinfo.value)
    assert "录音失败" in message and "/voice-devices" in message


def test_recorder_reports_an_unusable_device_in_chinese(monkeypatch):
    """A bad AGENT_VOICE_INPUT_DEVICE must fail while *choosing* the rate, not later."""

    class BrokenDevice(FakeSoundDevice):
        def check_input_settings(self, **kwargs) -> None:  # noqa: ANN003
            raise RuntimeError("Error querying device -1")

        def query_devices(self, device=None):  # noqa: ANN001, ARG002
            raise RuntimeError("Error querying device -1")

    sd = BrokenDevice(FakeStream([]))
    recorder = install_recorder(monkeypatch, sd)

    with pytest.raises(VoiceError) as excinfo:
        recorder.record_utterance()
    assert "录音失败" in str(excinfo.value)


def test_chat_flags_enable_voice_and_silence():
    from agent.cli.main import build_parser

    plain = build_parser().parse_args(["chat"])
    assert plain.voice is False and plain.no_speak is False
    voiced = build_parser().parse_args(["chat", "--voice", "--no-speak"])
    assert voiced.voice is True and voiced.no_speak is True


def test_recorder_without_sounddevice_raises_chinese(no_sounddevice):
    with pytest.raises(VoiceError) as excinfo:
        voice_module.SoundDeviceRecorder().record_utterance()
    assert "sounddevice 未安装" in str(excinfo.value)


def test_voice_loop_hands_free_records_until_ctrl_c(monkeypatch):
    monkeypatch.setattr(settings, "voice_tts", False)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    console = quiet_console()
    calls = {"count": 0}

    class HandsFreeRecorder:
        last_reason = "silence"

        def record_utterance(self) -> bytes | None:
            calls["count"] += 1
            if calls["count"] >= 3:
                raise KeyboardInterrupt
            return None

    def ask(prompt: str) -> str:  # pragma: no cover - hands-free never waits for Enter
        raise AssertionError("hands-free mode must not ask for a key press")

    mode = voice_loop(
        console=console,
        ask=ask,
        answer=lambda text: None,
        recorder=HandsFreeRecorder(),
        transcribe_fn=transcriber("x"),
        speak_fn=lambda text: True,
        hands_free=True,
    )

    assert mode == "off"
    assert calls["count"] == 3
    assert "免提模式" in console.export_text()


# ------------------------------------------------------------ console commands


@pytest.fixture(autouse=True)
def _never_persist(monkeypatch):
    """Voice settings are terminal-local: nothing may rewrite the VM's .env."""
    monkeypatch.setattr(
        SlashConsole,
        "_persist",
        lambda self, keys: (_ for _ in ()).throw(AssertionError(f"unexpected .env write: {keys}")),
    )


@pytest.fixture
def shell():
    console = Console(record=True, width=120, no_color=True, force_terminal=False)
    state = ConsoleState()
    return SlashConsole(console, state, console), console, state


@pytest.fixture
def no_sounddevice(monkeypatch):
    """Simulate "sounddevice is not installed" without uninstalling anything."""
    monkeypatch.setitem(sys.modules, "sounddevice", None)


def output(console: Console) -> str:
    # export_text() clears the record buffer by default, so the obvious two-line shape
    # ("assert A in output(c); assert B in output(c)") would silently lose A.  Every
    # caller here means "everything printed so far", so never clear.
    return console.export_text(clear=False)


def test_voice_mode_status_prints_chinese(shell):
    sh, console, _ = shell
    assert sh.handle("/voice-mode status") is True
    out = output(console)
    for word in ("语音模式", "输入设备", "最长录音", "朗读", "麦克风", "sounddevice"):
        assert word in out, f"status must mention {word}"


def test_voice_mode_off_and_status_tolerate_arguments(shell):
    sh, _, state = shell
    sh.handle("/voice-mode")
    assert state.voice_mode == "off"
    sh.handle("/voice-mode off")
    assert state.voice_mode == "off"


def test_voice_mode_rejects_an_unknown_action(shell):
    sh, console, state = shell
    sh.handle("/voice-mode maybe")
    assert "用法" in output(console)
    assert state.voice_mode == "off"


def test_voice_mode_on_off_and_hands_free(shell, monkeypatch):
    sh, _, state = shell
    import agent.cli.console as console_module

    monkeypatch.setattr(console_module, "sounddevice_available", lambda: (True, ""))
    sh.handle("/voice-mode on")
    assert state.voice_mode == "push-to-talk"
    sh.handle("/voice-mode hands-free")
    assert state.voice_mode == "hands-free"
    sh.handle("/voice-mode off")
    assert state.voice_mode == "off"


def test_voice_mode_on_without_sounddevice_stays_off(shell, no_sounddevice):
    sh, console, state = shell
    assert sh.handle("/voice-mode on") is True
    out = output(console)
    assert "sounddevice 未安装" in out
    assert "pip" in out, "the refusal must say how to install it"
    assert state.voice_mode == "off", "voice mode must not arm without a recorder"


def test_voice_devices_without_sounddevice_explains_itself(shell, no_sounddevice):
    sh, console, _ = shell
    assert sh.handle("/voice-devices") is True
    out = output(console)
    assert "sounddevice 未安装" in out
    assert "朗读" in out, "the TTS half of the report is still useful"


def test_voice_devices_lists_the_real_machine(shell):
    """On a machine with sounddevice this prints devices; without it, a Chinese reason."""
    sh, console, _ = shell
    assert sh.handle("/voice-devices") is True
    out = output(console)
    assert "AGENT_VOICE_INPUT_DEVICE" in out
    assert "sounddevice" in out


def test_list_input_devices_reports_a_missing_install(no_sounddevice):
    devices = list_input_devices()
    assert len(devices) == 1 and devices[0].get("error")
    assert "sounddevice 未安装" in devices[0]["error"]


def test_list_input_devices_always_returns_rows():
    devices = list_input_devices()
    assert devices, "callers must always get something to print"
    assert all(isinstance(row, dict) for row in devices)


def test_devices_table_renders_what_it_is_given():
    table = devices_table(
        [
            {"index": 3, "name": "麦克风阵列", "channels": 2, "samplerate": 48000, "api": "WASAPI", "default": True},
            {"index": 5, "name": "头戴麦", "channels": 1, "samplerate": 44100, "api": "MME", "default": False},
        ]
    )
    console = quiet_console()
    console.print(table)
    out = console.export_text()
    assert "麦克风阵列" in out and "头戴麦" in out
    assert "48000 Hz" in out and "WASAPI" in out
    assert "← 默认" in out


def test_speak_command_toggles_the_setting(shell, monkeypatch):
    sh, console, _ = shell
    monkeypatch.setattr(settings, "voice_tts", True)
    sh.handle("/speak off")
    assert settings.voice_tts is False
    assert "朗读：关" in output(console)
    sh.handle("/speak on")
    assert settings.voice_tts is True
    output(console)
    sh.handle("/speak")
    assert "朗读：开" in output(console)


def test_speak_command_rejects_a_bad_argument(shell, monkeypatch):
    sh, console, _ = shell
    monkeypatch.setattr(settings, "voice_tts", True)
    sh.handle("/speak maybe")
    assert "用法" in output(console)
    assert settings.voice_tts is True


def test_voice_help_is_discoverable_and_complete(shell):
    sh, console, _ = shell
    sh.handle("/help")
    overview = output(console)
    for command in ("/voice-mode", "/voice-devices", "/speak"):
        assert command in overview, f"{command} must be listed in /help"
    sh.handle("/help voice-mode")
    text = output(console)
    assert "/voice-mode on" in text
    assert "/voice-mode hands-free" in text
    assert "麦克风" in text
    assert "q" in text and "退出" in text
    # both talk keys the console actually binds (ctrl-t and f2 in main.build_console) are
    # discoverable from the help text, so the operator never has to guess
    assert TALK_KEY in text, "/help voice-mode names the primary talk key"
    assert TALK_KEY_ALT in text, "/help voice-mode names the alternate talk key"
    sh.handle("/help speak")
    assert "SAPI" in output(console)


def test_voice_mode_is_not_persisted_and_does_not_reach_the_service(shell, monkeypatch):
    sh, _, _ = shell
    import agent.cli.console as console_module

    monkeypatch.setattr(console_module, "sounddevice_available", lambda: (True, ""))

    def boom(changes=None):  # noqa: ANN001, ARG001
        raise AssertionError("voice mode is a terminal setting: it must not call /admin/config")

    monkeypatch.setattr(sh, "_admin", boom)
    for line in ("/voice-mode status", "/voice-mode on", "/voice-mode off", "/voice-devices", "/speak off", "/speak on"):
        sh.handle(line)
