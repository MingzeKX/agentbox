"""Real-time voice mode for the console: press Enter, speak, hear the answer.

``/voice <file>`` (see :mod:`agent.cli.console`) transcribes a recording that already
exists.  This module adds the *loop* around it -- the piece the operator asked for with
"类似语音助手的实时交互":

    Enter -> record from the microphone -> VAD finds the end of the utterance
          -> POST the WAV to the AI service's /asr (same token/base-url plumbing as
          /voice) -> hand the transcript to the REPL -> read the answer back aloud

Three constraints shaped the design:

* **the machine that runs the tests has no microphone, no speaker and no AI service**,
  so nothing here is wired to a device at import time: :class:`EnergyVad`,
  :func:`pack_wav` and :func:`build_speak_command` are pure functions,
  :class:`SoundDeviceRecorder` is one implementation of the :class:`Recorder` protocol
  (tests pass a fake), and :func:`voice_loop` takes its recorder, transcriber and
  speaker as arguments.
* **zero new Python dependencies for TTS**: Windows already ships SAPI
  (``System.Speech.Synthesis``), so the answer is spoken by a ``powershell.exe -Command``
  one-liner whose text is embedded and truncated -- the builder is pure so the exact
  command can be asserted without running PowerShell.
* **everything degrades in Chinese**: a missing ``sounddevice``, an unreachable AI
  service or a missing PowerShell is a clear message, never a traceback.

Push-to-talk is the default (one utterance per Enter -- or the explicit talk key).  A
microphone that is always open would let the assistant hear its own answer;
``/voice-mode hands-free`` opts into continuous capture for operators who want it.

Interruptibility
----------------
The loop is interruptible in both directions, because waiting is what made the first
version unusable ("改成可打断对话"):

* **talking over the answer**: :class:`SpeechHandle` keeps the SAPI process and polls
  :data:`keyboard` while it speaks.  Any keypress stops the speech immediately (the
  handle is stopped/terminated, so a long answer is never read to the end) and the loop
  goes straight back to listening;
* **cancelling a turn**: the loop survives ``Ctrl-C`` during a turn -- the caller's
  ``answer()`` cancels that turn and the loop asks again -- so one interrupted answer
  never ends the session.

The talk key exists because "press space to talk" was ambiguous: a lone space is text.
``Ctrl+T`` (or F2, at the caller's discretion) starts a recording *while a line is being
typed*; a blank line (bare Enter) does the same and stays the documented default.
"""

from __future__ import annotations

import array
import io
import logging
import math
import shutil
import subprocess
import sys
import time
import wave
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any, Protocol

import httpx
from rich.console import Console
from rich.table import Table
from rich.text import Text

from agent.config import settings

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
"""What faster-whisper wants: 16 kHz mono.  The recorder always returns this."""
SAMPLE_WIDTH = 2  #: int16
CHANNELS = 1
BLOCK_MS = 30
"""Block the VAD judges: small enough to notice a pause, big enough to be cheap."""
SPEAK_TIMEOUT_S = 300.0
"""SAPI blocks until it has finished speaking, so the ceiling has to be generous."""
HANDS_FREE_PAUSE_S = 0.2
"""Hands-free: breathe between a silent attempt and the next capture."""

#: how often a speaking answer checks the keyboard (barge-in latency, in seconds)
SPEECH_POLL_S = 0.03

#: the key that starts a recording while a line is being typed; also printed in the prompt
TALK_KEY = "Ctrl+T"
#: the same binding under its function-key name (both are bound where the terminal allows)
TALK_KEY_ALT = "F2"

#: what the push-to-talk prompt says: the two ways to talk, and how to leave
CONTINUE_PROMPT = f"按回车或 {TALK_KEY} 说话 · 输入 q 退出"

#: the three states the operator must be able to tell apart at a glance
RECORDING_MESSAGE = "🎤 录音中…（停顿 1.2 秒自动结束 / 按 Ctrl+C 取消）"
TRANSCRIBING_MESSAGE = "识别中…"
SPEAKING_MESSAGE = "🔊 朗读中（按任意键打断）"
INTERRUPTED_MESSAGE = "（已打断朗读，继续说）"
CANCELLED_MESSAGE = "（本轮已取消，继续说）"

STOP_WORDS = {"q", "quit", "exit", "/voice-mode off"}

#: the three modes the console can be in; shared with console.py / main.py
MODE_OFF = "off"
MODE_PUSH_TO_TALK = "push-to-talk"
MODE_HANDS_FREE = "hands-free"
MODE_LABELS = {
    MODE_OFF: "关",
    MODE_PUSH_TO_TALK: "开（按回车说话）",
    MODE_HANDS_FREE: "开（免提，麦克风常开）",
}

SOUNDDEVICE_MISSING = (
    "sounddevice 未安装（{error}）：实时语音模式需要它，请运行 "
    r".\.venv\Scripts\pip.exe install sounddevice numpy"
    "（或 pip install \"agentbox[voice]\"）"
)

VAD_REASONS = {
    "ok": "识别到语音",
    "silence": "没有听到声音（只检测到静音），请再试一次",
    "timeout": "一直没有听到说话，请靠近麦克风再试一次",
    "too-short": "只听到很短的一声（可能是噪声），请完整说一句再试",
    "max": "说话时间达到上限，已按最长录音时长截断",
}


class VoiceError(RuntimeError):
    """A voice-mode failure whose message is safe to show the operator (Chinese)."""


class TalkKey:
    """Sentinel a reader may return instead of a line: "start recording now".

    The REPL's line reader returns one of these when the operator presses the talk key
    (``Ctrl+T``/``F2``) *while typing*.  It is a sentinel rather than a magic string so a
    typed line can never be mistaken for it -- and ``" "`` in particular stays text.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "TalkKey()"


#: the singleton a reader returns (there are no attributes, so one instance suffices)
TALK_SENTINEL = TalkKey()


class Keyboard(Protocol):
    """A non-blocking console keyboard poll: used only to notice a barge-in."""

    def any_key_pressed(self) -> bool:
        """True when a key is waiting; the key itself is *not* consumed."""
        ...

    def discard(self) -> None:
        """Throw away buffered keypresses (stray keys must not interrupt a later answer)."""
        ...


class MsvcrtKeyboard:
    """``msvcrt``-based :class:`Keyboard` (Windows only; no-op everywhere else)."""

    def __init__(self) -> None:
        self.msvcrt = None
        try:
            import msvcrt  # noqa: PLC0415 - only exists on Windows

            self.msvcrt = msvcrt
        except Exception:  # noqa: BLE001 - any failure means "this console cannot poll"
            self.msvcrt = None

    @property
    def available(self) -> bool:
        return self.msvcrt is not None

    def any_key_pressed(self) -> bool:
        if self.msvcrt is None:
            return False
        try:
            return bool(self.msvcrt.kbhit())
        except Exception:  # noqa: BLE001 - a console without an input handle
            return False

    def discard(self) -> None:
        if self.msvcrt is None:
            return
        try:
            while self.msvcrt.kbhit():
                self.msvcrt.getch()
        except Exception:  # noqa: BLE001 - nothing to discard
            return


keyboard: Keyboard = MsvcrtKeyboard()
"""The process-wide key poll.  Tests replace it with a fake to prove barge-in."""


def any_key_pressed() -> bool:
    """True when a keypress is waiting on the console (never consumes a printable key)."""
    return keyboard.any_key_pressed()


def discard_pending_keys() -> None:
    """Drop keypresses buffered while an answer was being spoken or recorded."""
    keyboard.discard()


# --------------------------------------------------------------------------- WAV


def pack_wav(pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Frame raw little-endian int16 mono PCM as a 16-bit WAV the AI service can read."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def _to_samples(pcm: bytes) -> array.array:
    """int16 PCM -> a native-endian ``array('h')`` (the payload is little-endian)."""
    usable = len(pcm) - (len(pcm) % SAMPLE_WIDTH)
    samples = array.array("h")
    if usable:
        samples.frombytes(pcm[:usable])
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _from_samples(samples: array.array) -> bytes:
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def rms_int16(pcm: bytes) -> float:
    """Root-mean-square level of int16 PCM, normalised to 0..1 (0.0 for no samples)."""
    samples = _to_samples(pcm)
    if not samples:
        return 0.0
    total = 0
    for value in samples:
        total += value * value
    return math.sqrt(total / len(samples)) / 32768.0


def _linear_resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resample of mono int16 PCM -- pure Python, no numpy."""
    samples = _to_samples(pcm)
    if not samples:
        return b""
    out_length = max(1, int(len(samples) * dst_rate / src_rate))
    out = array.array("h", bytes(out_length * SAMPLE_WIDTH))
    ratio = src_rate / dst_rate
    last = len(samples) - 1
    for index in range(out_length):
        position = index * ratio
        left = int(position)
        if left >= last:
            out[index] = samples[last]
            continue
        fraction = position - left
        out[index] = int(samples[left] * (1.0 - fraction) + samples[left + 1] * fraction)
    return _from_samples(out)


def resample_int16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample mono int16 PCM to ``dst_rate``.

    Only used when a device refuses 16 kHz (WASAPI/WDM-KS often do).  numpy makes this
    ~100x faster, but it is optional: the pure-Python :func:`_linear_resample` is the
    fallback, so a recorder without numpy still works.
    """
    if src_rate == dst_rate or not pcm:
        return pcm
    try:
        import numpy  # noqa: PLC0415 - optional extra, imported only when resampling
    except Exception:  # noqa: BLE001 - any numpy problem falls back to pure Python
        return _linear_resample(pcm, src_rate, dst_rate)
    samples = numpy.frombuffer(pcm[: len(pcm) - (len(pcm) % SAMPLE_WIDTH)], dtype="<i2")
    out_length = max(1, int(len(samples) * dst_rate / src_rate))
    positions = numpy.arange(out_length) * (src_rate / dst_rate)
    left = numpy.floor(positions).astype(numpy.int64)
    right = numpy.minimum(left + 1, len(samples) - 1)
    fraction = positions - left
    mixed = samples[left] * (1.0 - fraction) + samples[right] * fraction
    return mixed.astype("<i2").tobytes()


# --------------------------------------------------------------------------- VAD


@dataclass
class VadConfig:
    """Thresholds of the energy VAD (all injectable, so tests need no audio)."""

    sample_rate: int = SAMPLE_RATE
    silence_s: float = 1.2
    """Quiet time after speech that ends the utterance (settings.voice_silence_s)."""
    max_s: float = 30.0
    """Hard cap on one utterance; longer speech is truncated (settings.voice_max_s)."""
    floor: float = 0.012
    """Absolute RMS floor: below it, nothing counts as speech (settings.voice_vad_floor)."""
    multiplier: float = 3.0
    """Speech must also be this many times louder than the measured noise floor."""
    calibration_s: float = 0.5
    """Head of the recording used to measure the room's noise floor."""
    min_speech_s: float = 0.3
    """Anything shorter than this is treated as noise, not an utterance."""
    preroll_s: float = 0.2
    """Audio kept from just before the first speech block, so the first syllable survives."""


@dataclass(frozen=True)
class VadResult:
    """What the VAD decided: the utterance PCM (``None`` when nothing was heard) and why."""

    pcm: bytes | None
    reason: str
    speech_s: float
    total_s: float
    noise_floor: float
    threshold: float

    @property
    def heard(self) -> bool:
        return self.pcm is not None


class EnergyVad:
    """Streaming energy VAD for 16 kHz mono int16 audio.

    Feed blocks in arrival order with :meth:`push`; it returns ``False`` as soon as the
    utterance is over, so a recorder can stop reading the microphone.  The noise floor is
    measured from the first :attr:`VadConfig.calibration_s`, and a block counts as speech
    when its level is above ``max(noise_floor * multiplier, absolute floor)`` -- the
    absolute term is what keeps a very quiet room from turning its own noise into speech.

    The class is deliberately clock-free and device-free: every decision is a function of
    the bytes pushed, which makes the whole endpointing behaviour unit-testable.
    """

    def __init__(self, config: VadConfig | None = None) -> None:
        self.config = config or VadConfig()
        self.started = False
        self.finished = False
        self._reason = "silence"
        self._noise_floor = 0.0
        self._calibrated = False
        self._calibration: list[float] = []
        self._blocks: list[bytes] = []
        self._recent: deque[bytes] = deque()
        self._recent_bytes = 0
        self._total_bytes = 0
        self._speech_bytes = 0
        self._silence_bytes = 0

    # ------------------------------------------------------------ introspection
    @property
    def noise_floor(self) -> float:
        """RMS measured during calibration (0.0 until calibration is over)."""
        return self._noise_floor

    @property
    def threshold(self) -> float:
        """The level a block has to beat to count as speech."""
        return max(self._noise_floor * self.config.multiplier, self.config.floor)

    @property
    def reason(self) -> str:
        return self._reason

    def _bytes_for(self, seconds: float) -> int:
        return max(0, int(seconds * self.config.sample_rate)) * SAMPLE_WIDTH

    # -------------------------------------------------------------------- state
    def push(self, block: bytes) -> bool:
        """Judge one block of audio; ``True`` while more audio is wanted."""
        if self.finished:
            return False
        block = block[: len(block) - (len(block) % SAMPLE_WIDTH)]
        if not block:
            return True
        self._total_bytes += len(block)
        level = rms_int16(block)

        if not self._calibrated:
            self._calibration.append(level)
            self._remember(block)
            if self._total_bytes >= self._bytes_for(self.config.calibration_s):
                self._calibrated = True
                self._noise_floor = sum(self._calibration) / len(self._calibration)
            return self._within_limits()

        if not self.started:
            if level >= self.threshold:
                self.started = True
                self._blocks = [*self._recent, block]  # pre-roll + this block
                self._speech_bytes = len(block)
                self._silence_bytes = 0
                self._recent.clear()
                self._recent_bytes = 0
            else:
                self._remember(block)
            return self._within_limits()

        self._blocks.append(block)
        if level >= self.threshold:
            self._speech_bytes += len(block)
            self._silence_bytes = 0
        else:
            self._silence_bytes += len(block)
            if self._silence_bytes >= self._bytes_for(self.config.silence_s):
                self._finalise("ok")
                return False
        return self._within_limits()

    def finish(self) -> VadResult:
        """End the stream (no more blocks are coming) and return the utterance."""
        if not self.finished:
            self._finalise("ok" if self.started else "silence")
        return self.result()

    def result(self) -> VadResult:
        """The current utterance: PCM capped at ``max_s``, or ``None`` when unusable."""
        pcm: bytes | None = None
        reason = self._reason
        if self.started and self._blocks:
            pcm = b"".join(self._blocks)[: self._bytes_for(self.config.max_s)]
            if self._speech_bytes < self._bytes_for(self.config.min_speech_s):
                pcm = None
                reason = "too-short"
        per_sample = SAMPLE_WIDTH * self.config.sample_rate
        return VadResult(
            pcm=pcm,
            reason=reason,
            speech_s=self._speech_bytes / per_sample,
            total_s=self._total_bytes / per_sample,
            noise_floor=self._noise_floor,
            threshold=self.threshold,
        )

    # ------------------------------------------------------------------ helpers
    def _remember(self, block: bytes) -> None:
        """Keep the newest ``preroll_s`` of audio (never more than one block extra)."""
        limit = self._bytes_for(self.config.preroll_s)
        if limit <= 0:
            return
        self._recent.append(block)
        self._recent_bytes += len(block)
        while len(self._recent) > 1 and self._recent_bytes > limit:
            self._recent_bytes -= len(self._recent.popleft())

    def _finalise(self, reason: str) -> None:
        self.finished = True
        self._reason = reason

    def _within_limits(self) -> bool:
        if self._total_bytes >= self._bytes_for(self.config.max_s):
            self._finalise("max" if self.started else "timeout")
            return False
        return True


def detect_utterance(blocks: Iterable[bytes], config: VadConfig | None = None) -> VadResult:
    """Run the VAD over a finite block sequence (the whole decision, no device involved)."""
    vad = EnergyVad(config)
    for block in blocks:
        if not vad.push(block):
            break
    return vad.finish()


def silence_message(reason: str) -> str:
    """Chinese explanation for "the recorder returned nothing"."""
    return VAD_REASONS.get(reason, VAD_REASONS["silence"])


# ----------------------------------------------------------------------- capture


class Recorder(Protocol):
    """A microphone that returns one utterance as 16 kHz mono int16 WAV bytes.

    ``None`` means "only silence was heard" -- the loop asks again instead of sending an
    empty recording to the AI service.  Implementations live in this module
    (:class:`SoundDeviceRecorder`) or in tests (a fake feeding synthetic buffers).
    """

    def record_utterance(self) -> bytes | None:
        ...


def _import_sounddevice() -> Any:
    """Import sounddevice on demand; a missing install becomes a Chinese VoiceError."""
    try:
        import sounddevice  # noqa: PLC0415 - optional extra, never imported at startup
    except Exception as exc:  # noqa: BLE001 - any import failure means "no audio here"
        raise VoiceError(SOUNDDEVICE_MISSING.format(error=f"{type(exc).__name__}: {exc}")) from exc
    return sounddevice


def sounddevice_available() -> tuple[bool, str]:
    """``(installed, Chinese reason)`` -- the reason is empty when it is installed."""
    try:
        _import_sounddevice()
    except VoiceError as exc:
        return False, str(exc)
    return True, ""


def device_argument(value: str | None) -> int | str | None:
    """``voice_input_device``: "" = system default, digits = index, anything else = name."""
    text = (value or "").strip()
    if not text:
        return None
    return int(text) if text.isdigit() else text


def list_input_devices() -> list[dict[str, Any]]:
    """Input devices sounddevice can see; a single ``{"error": ...}`` row when it cannot.

    Returns a list either way, so a caller can always print something useful.
    """
    try:
        sd = _import_sounddevice()
    except VoiceError as exc:
        return [{"error": str(exc)}]
    try:
        devices = sd.query_devices()
    except Exception as exc:  # noqa: BLE001 - PortAudio may have no host API at all
        return [{"error": f"无法枚举音频设备（{type(exc).__name__}: {exc}）"}]
    try:
        default_input = sd.default.device[0]
    except Exception:  # noqa: BLE001 - some backends report no default at all
        default_input = None
    rows: list[dict[str, Any]] = []
    for index, device in enumerate(devices):
        channels = int(device.get("max_input_channels") or 0)
        if channels <= 0:
            continue
        try:
            api = str(sd.query_hostapis(device["hostapi"])["name"])
        except Exception:  # noqa: BLE001 - the host API name is cosmetic
            api = ""
        rows.append(
            {
                "index": index,
                "name": str(device.get("name") or f"设备 {index}"),
                "channels": channels,
                "samplerate": int(device.get("default_samplerate") or 0),
                "api": api,
                "default": index == default_input,
            }
        )
    if not rows:
        return [{"error": "没有找到可用的输入设备（麦克风）——请插上麦克风，并检查 Windows 的麦克风权限"}]
    return rows


def devices_table(devices: list[dict[str, Any]]) -> Table:
    """Render :func:`list_input_devices` for the console."""
    table = Table(title="输入设备")
    table.add_column("序号", style="cyan", justify="right")
    table.add_column("名称")
    table.add_column("声道", justify="right")
    table.add_column("默认采样率", justify="right")
    table.add_column("接口")
    table.add_column("", style="green")
    for device in devices:
        table.add_row(
            str(device.get("index", "")),
            str(device.get("name", "")),
            str(device.get("channels", "")),
            f"{device.get('samplerate', 0)} Hz",
            str(device.get("api", "")),
            "← 默认" if device.get("default") else "",
        )
    return table


class SoundDeviceRecorder:
    """Microphone recorder: one utterance per call, always 16 kHz mono int16 WAV.

    The energy VAD does the endpointing, so the operator only speaks.  16 kHz is asked
    for first (that is what faster-whisper wants) and a device that refuses it is opened
    at its own rate and resampled, because WASAPI/WDM-KS regularly reject 16 kHz.
    """

    def __init__(
        self,
        device: str | None = None,
        *,
        silence_s: float | None = None,
        max_s: float | None = None,
        floor: float | None = None,
        block_ms: int = BLOCK_MS,
    ) -> None:
        self.device = device_argument(settings.voice_input_device if device is None else device)
        self.block_ms = block_ms
        self.vad_config = VadConfig(
            silence_s=settings.voice_silence_s if silence_s is None else silence_s,
            max_s=settings.voice_max_s if max_s is None else max_s,
            floor=settings.voice_vad_floor if floor is None else floor,
        )
        #: why the last call returned None (a VAD reason code), for the loop's message
        self.last_reason = ""

    def capture_rate(self, sd: Any) -> int:
        """16 kHz when the device takes it, else the device's own rate (resampled later)."""
        try:
            sd.check_input_settings(
                device=self.device, samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16"
            )
            return SAMPLE_RATE
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail
            info = sd.query_devices(self.device)
            rate = int(info.get("default_samplerate") or 0) or SAMPLE_RATE
            log.info("device %r refused %d Hz (%s); capturing at %d Hz", self.device, SAMPLE_RATE, exc, rate)
            return rate

    def record_utterance(self) -> bytes | None:
        """Record until the VAD hears the end of the utterance; ``None`` for silence only."""
        sd = _import_sounddevice()
        try:
            rate = self.capture_rate(sd)
            block_samples = max(1, int(rate * self.block_ms / 1000))
            # the VAD's thresholds are in seconds: tell it the rate it is really reading at
            vad = EnergyVad(replace(self.vad_config, sample_rate=rate))
            with sd.RawInputStream(
                samplerate=rate,
                channels=CHANNELS,
                dtype="int16",
                blocksize=block_samples,
                device=self.device,
            ) as stream:
                while True:
                    data, overflowed = stream.read(block_samples)
                    if overflowed:
                        log.warning("microphone overflow: blocks were dropped by the driver")
                    if not vad.push(bytes(data)):
                        break
        except Exception as exc:  # noqa: BLE001 - a device failure must read as Chinese
            raise VoiceError(
                f"录音失败（{type(exc).__name__}: {exc}）——"
                "用 /voice-devices 检查设备，或用 /voice-mode off 关闭语音模式"
            ) from exc
        result = vad.finish()
        self.last_reason = result.reason
        if result.pcm is None:
            return None
        pcm = result.pcm if rate == SAMPLE_RATE else resample_int16(result.pcm, rate, SAMPLE_RATE)
        return pack_wav(pcm)


# --------------------------------------------------------------------------- ASR


def asr_headers(media_type: str = "audio/wav") -> dict[str, str]:
    """Headers for ``POST /asr``: the control token plus the container's media type."""
    headers = {"Content-Type": media_type}
    if settings.control_secret:
        headers["X-Agent-Token"] = settings.control_secret
    return headers


def asr_url() -> str:
    return f"http://127.0.0.1:{settings.ai_port}/asr"


def transcribe(audio: bytes, *, media_type: str = "audio/wav", language: str | None = None) -> dict[str, Any]:
    """POST audio bytes to the AI service's ``/asr``; raises ``RuntimeError`` (Chinese).

    This is the single transcription path: ``/voice <file>`` and the live loop both use
    it, so the token, the URL and the error mapping exist exactly once.  ``language=None``
    means "the console's default": ``AGENT_ASR_LANGUAGE`` when it is set, else ``zh``
    (Whisper's auto-detect is unreliable on the short clips a voice turn produces).
    """
    if not audio:
        raise RuntimeError("音频是空的：没有录到任何数据")
    hint = (settings.asr_language or "zh") if language is None else language
    params = {"language": hint} if hint else None
    try:
        response = httpx.post(
            asr_url(), content=audio, headers=asr_headers(media_type), params=params, timeout=300.0
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"无法连接 AI 服务（语音识别）：{exc}") from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(f"语音识别返回了无法解析的响应（HTTP {response.status_code}）") from exc
    if response.status_code >= 400 or not body.get("ok"):
        reason = body.get("error") or f"HTTP {response.status_code}"
        if response.status_code == 503:
            raise RuntimeError(
                f"语音识别不可用：{reason}（在 VM 里检查 AGENT_ASR_ENABLED，并安装 agentbox[asr]）"
            )
        if response.status_code == 401:
            raise RuntimeError("语音识别被拒绝：控制面令牌不匹配（检查 AGENT_CONTROL_SECRET）")
        raise RuntimeError(f"语音识别失败：{reason}")
    return body


# --------------------------------------------------------------------------- TTS


def flatten_for_speech(text: str) -> str:
    """Turn an answer into something that reads well out loud.

    Markdown emphasis (backticks, asterisks) and newlines are noise for a synthesiser, so
    they are collapsed into plain single-spaced text.
    """
    return " ".join(str(text or "").replace("`", "").replace("*", "").split())


def truncate_for_speech(text: str, max_chars: int) -> str:
    """Flatten ``text`` for a speech synthesiser and cut it to ``max_chars`` characters.

    The result is never longer than ``max_chars``; a truncation ends in "…".
    """
    plain = flatten_for_speech(text)
    if max_chars <= 0:
        return ""
    if len(plain) <= max_chars:
        return plain
    if max_chars == 1:
        return "…"
    return plain[: max_chars - 1] + "…"


def _escape_single_quotes(text: str) -> str:
    """Inside a PowerShell single-quoted string only ``'`` needs doubling."""
    return text.replace("'", "''")


def build_speak_command(text: str, *, max_chars: int, executable: str = "powershell") -> list[str]:
    """The ``powershell.exe`` command that speaks ``text`` through SAPI (pure, runs nothing).

    Kept separate from :func:`speak` so the escaping and the truncation can be asserted
    without making a sound: tests never execute PowerShell.
    """
    spoken = _escape_single_quotes(truncate_for_speech(text, max_chars))
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Speak('{spoken}'); "
        "$s.Dispose()"
    )
    return [executable, "-NoProfile", "-NonInteractive", "-Command", script]


def powershell_executable() -> str | None:
    """``powershell.exe`` (Windows PowerShell 5.1, which ships SAPI), else ``None``."""
    for name in ("powershell.exe", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _run_command(command: list[str], *, timeout: float = SPEAK_TIMEOUT_S) -> bool:
    """Run one PowerShell command; False on any failure (the message stays in the log).

    ``errors="replace"`` matters: PowerShell writes its own messages in the console code
    page, so decoding them strictly raises UnicodeDecodeError on a machine whose locale
    cannot represent the bytes -- and the exit code is all this call really needs.
    """
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("SAPI call failed: %s", exc)
        return False
    if completed.returncode != 0:
        log.warning("SAPI returned %s: %s", completed.returncode, (completed.stderr or "").strip()[:300])
    return completed.returncode == 0


def _say(console: Console | None, message: str) -> None:
    if console is not None:
        console.print(message)


class SpeechHandle:
    """A SAPI utterance that can be interrupted while it is still being spoken.

    ``speak()`` starts the PowerShell/SAPI process and hands back one of these; the voice
    loop polls :meth:`wait` with a 30 ms tick, so a keypress ends the speech immediately
    instead of waiting for a long answer to be read out.  Two seams keep this testable
    without a speaker: the process object and the keyboard poll.
    """

    def __init__(self, process: Any = None, *, done: bool = False, ok: bool = True) -> None:
        self.process = process
        self._done = done
        self._ok = ok
        self._stopped = False

    @property
    def ok(self) -> bool:
        """False when the utterance already failed (SAPI error, no audio device)."""
        return self._ok

    def poll(self) -> bool | None:
        """``None`` while it is speaking, else whether it was interrupted."""
        if self._stopped:
            return True
        if self._done:
            return False
        if self.process is None:
            return False
        code = self.process.poll()
        if code is None:
            return None
        self._done = True
        if code != 0:
            log.warning("SAPI returned %s", code)
        return False

    def request_interrupt(self) -> bool:
        """True when a keypress is waiting: the answer must stop right now."""
        return any_key_pressed()

    def stop(self) -> None:
        """Kill the utterance (and therefore the speech) immediately."""
        self._stopped = True
        process = self.process
        if process is None:
            return
        try:
            process.terminate()
        except Exception as exc:  # noqa: BLE001 - already gone, or not killable
            log.debug("could not terminate SAPI: %s", exc)
        try:
            process.wait(timeout=2)
        except Exception:  # noqa: BLE001 - terminate is best effort; never block the loop
            log.debug("SAPI did not exit after terminate()")

    def wait(self, poll_s: float = SPEECH_POLL_S, timeout: float = SPEAK_TIMEOUT_S) -> bool:
        """Block until the speech ends or a key arrives; True means "barge-in"."""
        deadline = time.monotonic() + timeout
        while True:
            interrupted = self.poll()
            if interrupted is not None:
                return interrupted
            if self.request_interrupt():
                self.stop()
                return True
            if time.monotonic() >= deadline:
                log.warning("Speech exceeded %.0f s; stopping it", timeout)
                self.stop()
                return True
            time.sleep(poll_s)

    # ``run()`` is the old call shape ("did the command succeed?"): a fake runner may
    # return a bool, and speak() adapts it into a finished handle.
    @classmethod
    def finished(cls, ok: bool) -> SpeechHandle:
        return cls(done=True, ok=ok)


def start_speech(command: list[str], *, timeout: float = SPEAK_TIMEOUT_S) -> SpeechHandle:
    """Start ``command`` (the SAPI one-liner) in the background and return its handle."""
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed binary, explicit argv
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("SAPI call failed: %s", exc)
        return SpeechHandle.finished(False)
    return SpeechHandle(process)


def _as_handle(result: Any) -> SpeechHandle:
    """Normalise a runner's return value: a handle is used as-is, a bool becomes "done"."""
    if isinstance(result, SpeechHandle):
        return result
    if result is None:
        return SpeechHandle.finished(True)
    return SpeechHandle.finished(bool(result))


def say_with_barge_in(
    text: str,
    *,
    console: Console | None = None,
    runner: Callable[[list[str]], Any] | None = None,
    poll_s: float = SPEECH_POLL_S,
) -> bool:
    """Speak ``text`` and stay interruptible; True means "a key stopped the speech".

    The caller gets its answer back as soon as a key is pressed -- the process behind the
    utterance is terminated, so the operator never has to wait out a long answer.  The
    ``runner`` seam keeps the tests free of PowerShell: pass
    ``lambda command: SpeechHandle(fake_process)`` and the fake process decides when the
    speech is over.
    """
    if not settings.voice_tts:
        _say(console, "[dim]朗读已关闭（用 [/dim][cyan]/speak on[/cyan][dim] 打开）[/dim]")
        return False
    spoken = truncate_for_speech(text, settings.voice_tts_max_chars)
    if not spoken:
        _say(console, "[yellow]没有可朗读的文本[/yellow]")
        return False
    executable = powershell_executable()
    if executable is None:
        _say(console, "[yellow]找不到 powershell.exe：SAPI 朗读只在 Windows 上可用（回答已在上方）[/yellow]")
        return False
    if len(spoken) < len(flatten_for_speech(text)):
        _say(console, f"[dim]（朗读已截断到 {settings.voice_tts_max_chars} 字）[/dim]")
    command = build_speak_command(text, max_chars=settings.voice_tts_max_chars, executable=executable)
    _say(console, SPEAKING_MESSAGE)
    handle = _as_handle(runner(command) if runner else start_speech(command))
    if not handle.ok:
        _say(console, "[yellow]朗读失败：SAPI 报错或没有可用的音频输出设备（回答已在上方）[/yellow]")
        return False
    interrupted = handle.wait(poll_s=poll_s)
    if interrupted:
        _say(console, f"[dim]{INTERRUPTED_MESSAGE}[/dim]")
    return interrupted


def speak(
    text: str,
    *,
    console: Console | None = None,
    runner: Callable[[list[str]], Any] | None = None,
) -> bool:
    """Read ``text`` aloud through Windows SAPI; ``False`` = not spoken, with a reason.

    ``runner`` is the seam tests use: they pass a recorder that raises if it is called, so
    "TTS is off" and "there is nothing to say" are proven to never touch PowerShell.  A
    runner may return a plain bool (the command finished) or a :class:`SpeechHandle`,
    which is what the interruptible loop uses.
    """
    if not settings.voice_tts:
        _say(console, "[dim]朗读已关闭（用 [/dim][cyan]/speak on[/cyan][dim] 打开）[/dim]")
        return False
    spoken = truncate_for_speech(text, settings.voice_tts_max_chars)
    if not spoken:
        _say(console, "[yellow]没有可朗读的文本[/yellow]")
        return False
    executable = powershell_executable()
    if executable is None:
        _say(console, "[yellow]找不到 powershell.exe：SAPI 朗读只在 Windows 上可用（回答已在上方）[/yellow]")
        return False
    if len(spoken) < len(flatten_for_speech(text)):
        _say(console, f"[dim]（朗读已截断到 {settings.voice_tts_max_chars} 字）[/dim]")
    command = build_speak_command(text, max_chars=settings.voice_tts_max_chars, executable=executable)
    handle = _as_handle(runner(command) if runner else _run_command(command))
    if not handle.ok:
        _say(console, "[yellow]朗读失败：SAPI 报错或没有可用的音频输出设备（回答已在上方）[/yellow]")
        return False
    # ``speak`` is the blocking shape: it waits for the utterance and reports "spoken".
    # The voice loop calls say_with_barge_in() instead, which returns "was interrupted".
    return not handle.wait()


# -------------------------------------------------------------------------- loop


def flush_console_input() -> None:
    """Drop type-ahead so a keypress from *before* a recording cannot cancel it."""
    try:
        import msvcrt  # noqa: PLC0415 - Windows only

        while msvcrt.kbhit():
            msvcrt.getch()
    except Exception:  # noqa: BLE001 - a console without an input handle has nothing to flush
        return


def voice_loop(
    *,
    console: Console,
    ask: Callable[[str], str | TalkKey],
    answer: Callable[[str], str | None],
    recorder: Recorder | None = None,
    transcribe_fn: Callable[..., dict[str, Any]] | None = None,
    speak_fn: Callable[[str], bool] | None = None,
    hands_free: bool = False,
) -> str:
    """One voice session: record, transcribe, answer, read the answer back aloud.

    ``ask(prompt)`` reads one line from the operator and ``answer(text)`` hands the
    transcript to the REPL -- which prints the reply -- and returns that reply so it can be
    spoken.  Returns the mode to keep, which is currently always ``"off"``: every way out
    (``q``, Ctrl-C at the prompt, ``/voice-mode off``, end of input) ends voice mode.

    Interruption, the operator's first complaint:

    * a blank line (bare Enter) or :data:`TALK_SENTINEL` (the talk key, pressed while
      typing) records one utterance -- a lone space is text, never a trigger;
    * any keypress while the answer is being spoken stops the speech and goes back to
      listening (barge-in), instead of waiting for a long answer to be read out;
    * Ctrl-C *during a turn* cancels that turn and asks again; only Ctrl-C at the prompt
      (or ``q``) leaves the mode, so one interrupted answer never ends the session.

    Typing (instead of pressing Enter) sends that text as a normal message, so voice mode
    never traps the operator into a microphone-only interface.
    """
    recorder = recorder or SoundDeviceRecorder()
    transcribe_fn = transcribe_fn or transcribe
    # resolved here (not as a default argument) so tests can monkeypatch voice.speak
    speak_answer: Callable[[str], bool] = speak_fn or (
        lambda text: say_with_barge_in(text, console=console)
    )

    if hands_free:
        console.print("[dim]免提模式：直接说话，停顿一下自动结束；Ctrl-C 退出语音模式[/dim]")
    else:
        # say up front how to talk: bare Enter, or the talk key while a line is being typed
        console.print(f"[dim]{CONTINUE_PROMPT}[/dim]")
    while True:
        typed: str | None = None
        if not hands_free:
            # the talk key arrives as a sentinel, so a typed space can never mean "record"
            trigger = ask(CONTINUE_PROMPT)
            if isinstance(trigger, TalkKey):
                line = ""
            else:
                line = str(trigger).strip()
            if line.lower() in STOP_WORDS:
                console.print("[dim]已退出语音模式[/dim]")
                return "off"
            if line.startswith("/"):
                console.print(
                    "[yellow]语音模式下只认 q（退出）或直接输入文字；"
                    "其他命令请先用 q 退出语音模式[/yellow]"
                )
                continue
            typed = line or None  # only a truly empty line means "record"

        if typed is None:
            console.print(RECORDING_MESSAGE)
            flush_console_input()
            try:
                wav = recorder.record_utterance()
            except VoiceError as exc:
                console.print(Text(str(exc), style="red"))
                return "off"
            except KeyboardInterrupt:
                console.print()
                console.print("[dim]已取消这次录音（继续说，或输入 q 退出语音模式）[/dim]")
                continue
            if wav is None:
                console.print(Text(silence_message(getattr(recorder, "last_reason", "")), style="yellow"))
                if hands_free:
                    time.sleep(HANDS_FREE_PAUSE_S)
                continue
            console.print(TRANSCRIBING_MESSAGE)
            try:
                body = transcribe_fn(wav)
            except RuntimeError as exc:
                # an exception message can contain "[" (a URL, a regex): never re-parse it
                console.print(Text(str(exc), style="red"))
                continue
            text = str(body.get("text") or "").strip()
            if not text:
                console.print("[yellow]没有识别到语音内容（录音太短或全是静音？）[/yellow]")
                continue
            console.print(f"[bold cyan]识别到：[/bold cyan]{text}")
        else:
            text = typed

        try:
            reply = answer(text)
        except KeyboardInterrupt:
            # "while the agent is generating, Ctrl-C must cancel that turn and return to
            # the prompt without killing the console": the turn already stopped itself
            # (see main._stream_turn), so this only has to keep the session alive.
            console.print()
            console.print(f"[dim]{CANCELLED_MESSAGE}[/dim]")
            continue
        if reply and settings.voice_tts and speak_answer(reply):
            # the operator cut the answer short: drop the type-ahead that did it, so the
            # next prompt starts with a clean keyboard buffer
            discard_pending_keys()
    return "off"

