"""Local speech-to-text (ASR) for the AI service.

The endpoint (``POST /asr``) hands raw audio bytes to :class:`Transcriber`, which
runs `faster-whisper`_ (CTranslate2) on the service's CPU.  Two hard rules from
``tests/unit/test_isolation_guard.py`` shape this module:

* no filesystem, no ``open()``, no subprocess - the library is given an
  :class:`io.BytesIO` (and the decoded samples), so a transcript never needs a
  temporary file;
* the heavy call runs in :func:`asyncio.to_thread`, so one long transcription
  cannot stall the event loop that serves ``/chat``.

Nothing heavy is imported at module import time: ``faster_whisper`` is pulled in
on the first :attr:`Transcriber.available` / :meth:`Transcriber.transcribe` call,
so a missing install (or a slow model load) never delays service start.

Configuration (:class:`agent.config.Settings`):

* ``AGENT_ASR_ENABLED``      - master switch (default ``true``)
* ``AGENT_ASR_MODEL``        - ``tiny`` | ``base`` | ``small`` | ``medium`` (default ``base``)
* ``AGENT_ASR_COMPUTE_TYPE`` - CTranslate2 compute type (default ``int8``)
* ``AGENT_ASR_LANGUAGE``     - ``""`` = auto-detect (default)
* ``AGENT_ASR_MAX_SECONDS``  - refuse longer audio (default ``300``)
* ``AGENT_ASR_LOCAL_ONLY``   - read the HF cache only, never download (default ``true``)

.. _faster-whisper: https://github.com/SYSTRAN/faster-whisper
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import Any

from agent.config import settings

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
"""faster-whisper always decodes to 16 kHz mono float32 (its own constant)."""

MAX_LANGUAGE_CHARS = 8

#: placeholder response fields, so every return value has the same shape
_EMPTY: dict[str, Any] = {"ok": False, "text": "", "language": "", "duration_s": 0.0, "error": None}


class ASRError(RuntimeError):
    """A transcription failure whose message is safe to show the caller."""


class AudioTooLong(ASRError):
    """The audio exceeds ``AGENT_ASR_MAX_SECONDS``."""


def _decode_audio(data: bytes):
    """Decode compressed audio bytes into 16 kHz mono float32 samples.

    Imported lazily (PyAV/ctranslate2 are heavy) and handed a file-like object:
    the AI service is not allowed to read paths at all.
    """
    from faster_whisper import decode_audio

    return decode_audio(io.BytesIO(data))


def _normalise_language(language: str | None) -> str | None:
    """``""``/``auto``/``None`` mean auto-detect; anything else is lower-cased."""
    text = (language or "").strip().lower()[:MAX_LANGUAGE_CHARS]
    return None if text in {"", "auto", "detect"} else text


class Transcriber:
    """Lazily loaded faster-whisper wrapper (one process-wide instance)."""

    def __init__(
        self,
        model_name: str | None = None,
        compute_type: str | None = None,
        language: str | None = None,
        max_seconds: float | None = None,
        device: str = "cpu",
        local_files_only: bool | None = None,
    ) -> None:
        self.model_name = model_name or settings.asr_model
        self.compute_type = compute_type or settings.asr_compute_type
        self.language = settings.asr_language if language is None else language
        self.max_seconds = settings.asr_max_seconds if max_seconds is None else max_seconds
        self.device = device
        self.local_files_only = settings.asr_local_only if local_files_only is None else local_files_only
        self._model: Any = None
        self._load_error: str | None = None
        self._import_ok: bool | None = None
        # serialises inference: the model is CPU bound and _load is not re-entrant
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ availability
    @property
    def enabled(self) -> bool:
        return bool(settings.asr_enabled)

    @property
    def available(self) -> bool:
        """True when the model library can be imported.  Cached; never raises."""
        if self._import_ok is None:
            try:
                import faster_whisper  # noqa: F401  (heavy: first probe/use only)
            except Exception as exc:  # noqa: BLE001 - any failure means "no local ASR"
                self._import_ok = False
                self._load_error = (
                    f"faster-whisper is not importable ({type(exc).__name__}: {exc}); "
                    "install it with: pip install 'agentbox[asr]'"
                )
            else:
                self._import_ok = True
        return self._import_ok

    @property
    def import_status(self) -> bool | None:
        """Cached import probe without probing: True/False, or None if never checked."""
        return self._import_ok

    def unavailable_reason(self) -> str:
        """Why :attr:`available` is False (empty string when it is True)."""
        if self.available:
            return ""
        return self._load_error or "faster-whisper is not available"

    # ------------------------------------------------------------------- model
    def _load(self):
        """Build the CTranslate2 model (called from a worker thread)."""
        if self._model is not None:
            return self._model
        if not self.available:
            raise ASRError(self.unavailable_reason())
        from faster_whisper import WhisperModel

        started = time.monotonic()
        log.info(
            "loading ASR model %s (device=%s, compute_type=%s, local_files_only=%s)",
            self.model_name,
            self.device,
            self.compute_type,
            self.local_files_only,
        )
        try:
            model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
                local_files_only=self.local_files_only,
            )
        except Exception as exc:  # noqa: BLE001 - report any load failure as data
            self._load_error = (
                f"cannot load ASR model {self.model_name!r} ({type(exc).__name__}: {exc}); "
                "pre-download it into HF_HOME (e.g. HF_HOME=/opt/agentbox/models "
                f"python -c \"from faster_whisper import WhisperModel; WhisperModel('{self.model_name}')\")"
            )
            raise ASRError(self._load_error) from exc
        log.info("ASR model %s ready in %.1fs", self.model_name, time.monotonic() - started)
        self._model = model
        return model

    # ------------------------------------------------------------ transcription
    def _transcribe_sync(self, audio: bytes, language: str | None) -> dict[str, Any]:
        """Decode + run inference.  Blocking; only ever called through a thread."""
        samples = _decode_audio(audio)
        duration = len(samples) / SAMPLE_RATE
        if self.max_seconds and duration > self.max_seconds:
            raise AudioTooLong(
                f"audio is {duration:.1f}s long; AGENT_ASR_MAX_SECONDS is {self.max_seconds:.0f}s"
            )
        model = self._load()
        segments, info = model.transcribe(
            samples,
            language=_normalise_language(language) or _normalise_language(self.language),
            beam_size=1,
            condition_on_previous_text=False,
        )
        text = "".join(segment.text for segment in segments).strip()
        detected = getattr(info, "language", None) or language or self.language or ""
        return {"text": text, "language": str(detected), "duration_s": float(duration)}

    async def transcribe(
        self,
        audio: bytes,
        *,
        language: str | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Transcribe ``audio`` bytes.  Never raises; failures come back as ``ok: False``.

        ``filename`` is only used for logging (the bytes are all that is read).
        """
        result = dict(_EMPTY)
        result["model"] = self.model_name
        if not audio:
            result["error"] = "empty audio body"
            return result
        if not self.enabled:
            result["error"] = "ASR is disabled (AGENT_ASR_ENABLED=false)"
            return result
        if not self.available:
            result["error"] = self.unavailable_reason()
            return result

        started = time.monotonic()
        try:
            async with self._lock:
                out = await asyncio.to_thread(self._transcribe_sync, audio, language)
        except AudioTooLong as exc:
            result["error"] = str(exc)
            return result
        except Exception as exc:  # noqa: BLE001 - a broken model must not kill the request loop
            log.exception("ASR transcription failed (model=%s, filename=%s)", self.model_name, filename)
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result

        result.update(ok=True, **out)
        log.info(
            "asr transcribed %d bytes in %.1fs (model=%s, language=%s, filename=%s)",
            len(audio),
            time.monotonic() - started,
            self.model_name,
            result["language"],
            filename,
        )
        return result


_transcriber: Transcriber | None = None


def get_transcriber() -> Transcriber:
    """Process-wide singleton (the model is ~150 MB of weights; load it once)."""
    global _transcriber
    if _transcriber is None:
        _transcriber = Transcriber()
    return _transcriber


def set_transcriber(transcriber: Transcriber | None) -> None:
    """Test hook / dependency injection."""
    global _transcriber
    _transcriber = transcriber
