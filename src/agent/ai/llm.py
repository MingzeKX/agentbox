"""OpenAI compatible chat client with tool calling and incremental streaming.

Implemented directly on httpx (no vendor SDK) so that the only outbound traffic
the AI service performs is HTTPS to the LLM endpoint -- it never opens a socket
to a sandbox, and it never touches host files or processes.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from agent.ai.history import GATEWAY_BODY_MAX_CHARS, sanitize_history
from agent.config import settings

log = logging.getLogger(__name__)

#: image containers accepted inside a ``data:`` URL.  Deliberately small: the gateway
#: (and the vision model behind it) only decodes these four.
IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})

#: sloppy spellings clients send, mapped onto the canonical type above
IMAGE_MEDIA_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg", "image/x-png": "image/png"}

#: Model ids this deployment *knows* accept image input (``/models`` reports
#: ``input_modalities`` containing ``"image"`` for ``deepseek-flash``).  Kept as a
#: constant because the /chat path must not spend a ``/models`` round trip per turn.
KNOWN_VISION_MODELS = frozenset({"deepseek-flash"})

#: Substrings that mark a model as multimodal even when it is not in the list above, so
#: an operator pointing AGENT_LLM_MODEL at e.g. ``gpt-4o`` or ``qwen-vl-max`` is not
#: silently switched to the deployment default vision model.
VISION_MODEL_HINTS = ("vision", "-vl", "vl-", "gpt-4o", "gpt-4.1", "gemini", "claude-3", "claude-4")

#: Ids reported by ``GET /llm`` when the provider's ``/models`` call fails.  These are the
#: two ids this gateway actually serves (confirmed with ``GET {base_url}/models``), so a
#: CLI model picker stays useful while the gateway is briefly unreachable.
FALLBACK_MODEL_IDS = ("deepseek-flash", "deepseek-v4-pro")


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body

    def detail(self, limit: int = GATEWAY_BODY_MAX_CHARS) -> str:
        """The line the operator sees: our message *plus* the gateway's own words.

        A bare ``LLM endpoint returned HTTP 400`` is unactionable -- the gateway explains
        exactly which field it rejected, so that explanation is what the console shows
        (whitespace collapsed and truncated: it is usually a JSON error object).
        """
        if not self.body:
            return str(self)
        body = " ".join(str(self.body).split())
        if len(body) > limit:
            body = body[: limit - 1] + "…"
        return f"{self} — {body}"


class ImageInputError(ValueError):
    """Bad image payload: the message is shown to the caller, ``status`` becomes the HTTP code.

    413 for a payload that is simply too big, 422 for anything malformed (type, base64,
    count), 503 when image input has been switched off entirely.
    """

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def canonical_media_type(media_type: str) -> str:
    """Normalise and check an image media type against :data:`IMAGE_MEDIA_TYPES`."""
    candidate = (media_type or "").strip().lower().split(";")[0].strip()
    candidate = IMAGE_MEDIA_ALIASES.get(candidate, candidate)
    if candidate not in IMAGE_MEDIA_TYPES:
        raise ImageInputError(
            f"unsupported image media type {media_type!r}; send one of: {', '.join(sorted(IMAGE_MEDIA_TYPES))}"
        )
    return candidate


def image_part(raw: bytes, media_type: str = "image/png", *, max_bytes: int | None = None) -> dict[str, Any]:
    """One OpenAI-style ``image_url`` content part built from raw bytes.

    The bytes travel inline as ``data:<mime>;base64,<...>``: the AI service has no way to
    upload a file (it never touches the filesystem), so inlining is the only shape the
    gateway can read.
    """
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise ImageInputError("image payload is empty")
    data = bytes(raw)
    limit = settings.llm_max_image_bytes if max_bytes is None else max_bytes
    if len(data) > limit:
        raise ImageInputError(
            f"image is {len(data)} bytes, the limit is {limit} bytes (AGENT_LLM_MAX_IMAGE_BYTES)", status=413
        )
    mime = canonical_media_type(media_type)
    encoded = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def build_image_parts(
    images: Sequence[tuple[bytes, str]],
    *,
    max_images: int | None = None,
    max_bytes: int | None = None,
) -> list[dict[str, Any]]:
    """Validate the count/size/type of every image, then turn all of them into parts."""
    if not settings.llm_vision_enabled:
        raise ImageInputError("image input is disabled (AGENT_LLM_VISION_ENABLED=false)", status=503)
    limit = settings.llm_max_images if max_images is None else max_images
    if len(images) > limit:
        raise ImageInputError(f"{len(images)} images were sent, the limit is {limit} (AGENT_LLM_MAX_IMAGES)")
    return [image_part(raw, media_type, max_bytes=max_bytes) for raw, media_type in images]


def user_content(
    text: str,
    images: Sequence[tuple[bytes, str]] | None = None,
    *,
    max_images: int | None = None,
    max_bytes: int | None = None,
) -> str | list[dict[str, Any]]:
    """The user message content: a plain string for text-only, else text + image parts."""
    if not images:
        return text
    return [{"type": "text", "text": text}, *build_image_parts(images, max_images=max_images, max_bytes=max_bytes)]


def decode_base64_image(data_base64: str) -> bytes:
    """Decode a client-supplied base64 image, refusing anything that is not base64."""
    try:
        return base64.b64decode((data_base64 or "").strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageInputError(f"image data_base64 is not valid base64: {exc}") from exc


def content_has_image(content: Any) -> bool:
    """True when a message content is a part list containing an ``image_url`` part."""
    if not isinstance(content, list):
        return False
    return any(isinstance(part, dict) and part.get("type") == "image_url" for part in content)


def messages_have_images(messages: Sequence[dict[str, Any]]) -> bool:
    return any(content_has_image(message.get("content")) for message in messages if isinstance(message, dict))


def accepts_images(model: str) -> bool:
    """Best-effort guess that a model id already sees images (no network call)."""
    lowered = (model or "").strip().lower()
    if lowered in KNOWN_VISION_MODELS:
        return True
    return any(hint in lowered for hint in VISION_MODEL_HINTS)


def choose_model(model: str, messages: Sequence[dict[str, Any]]) -> str:
    """Swap in the vision model for a turn that carries images.

    Why: on this gateway ``deepseek-v4-pro`` (and the configured ``deepseek-chat``) accept a
    body containing ``image_url`` parts but silently ignore them -- the reply is empty.  Only
    ``deepseek-flash`` reports ``input_modalities: ["text", "image"]``, so a request with
    images has to be routed there unless the selected model already accepts images.  The
    switch is per turn, so the next text-only turn goes back to the configured model.
    """
    if not settings.llm_vision_enabled or not messages_have_images(messages):
        return model
    if accepts_images(model) or model == settings.llm_vision_model:
        return model
    return settings.llm_vision_model


@dataclass
class LLMToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""

    def to_message_part(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.raw_arguments or json.dumps(self.arguments)},
        }


@dataclass
class LLMResult:
    content: str = ""
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""


def _parse_arguments(raw: str) -> dict[str, Any]:
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


class LLMClient:
    """Chat client for any OpenAI compatible endpoint (DeepSeek by default)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.model = model or settings.llm_model
        self.timeout_s = timeout_s or settings.llm_timeout_s

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        model: str | None,
        stream: bool,
    ) -> dict[str, Any]:
        # the single choke point every request goes through: whatever the caller assembled
        # (a row written by an older version, a tool result that is megabytes wide, a
        # history window that cut a tool_call/tool result pair in half) is made safe here,
        # so one bad message can never poison a session again
        safe_messages = sanitize_history(messages)
        payload: dict[str, Any] = {
            "model": choose_model(model or self.model, safe_messages),
            "messages": safe_messages,
            "temperature": temperature,
            "stream": stream,
        }
        # Pass-through only.  Measured on this gateway: a bogus value is accepted, and a
        # 3-run reasoning-token average gave low 104 / high 156 / max 124 -- i.e. the
        # knob's effect is not reliably measurable here.  Never branch on it locally.
        if settings.llm_effort:
            payload["effort"] = settings.llm_effort
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        model: str | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> LLMResult:
        """One completion round.  ``on_token`` receives content deltas as they arrive.

        A message's ``content`` may be a plain string (unchanged behaviour) or a list of
        parts: ``{"type": "text", "text": ...}`` / ``{"type": "image_url", ...}``.
        """
        if not self.api_key:
            raise LLMError("AGENT_LLM_API_KEY is not set")
        payload = self._payload(
            messages,
            tools,
            settings.llm_temperature if temperature is None else temperature,
            model,
            stream=True,
        )
        result = LLMResult(model=payload["model"])
        calls: dict[int, dict[str, Any]] = {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                async with client.stream("POST", self.endpoint, json=payload, headers=self._headers()) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        raise LLMError(
                            f"LLM endpoint returned HTTP {response.status_code}", response.status_code, body[:1000]
                        )
                    async for line in response.aiter_lines():
                        chunk = _parse_sse_line(line)
                        if chunk is None:
                            continue
                        if chunk == "[DONE]":
                            break
                        _merge_chunk(chunk, result, calls, on_token)
        except httpx.HTTPError as exc:
            raise LLMError(f"cannot reach the LLM endpoint: {exc}") from exc

        for index in sorted(calls):
            entry = calls[index]
            result.tool_calls.append(
                LLMToolCall(
                    id=entry.get("id") or f"call_{index}",
                    name=entry.get("name") or "",
                    arguments=_parse_arguments(entry.get("arguments") or ""),
                    raw_arguments=entry.get("arguments") or "{}",
                )
            )
        return result

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[LLMResult]:
        """Yield the (single) aggregated result; kept for API symmetry with callers."""
        yield await self.chat(messages, tools=tools, model=model)

    async def models(self) -> list[str]:
        url = f"{self.base_url}/models"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._headers())
        if response.status_code >= 400:
            raise LLMError(f"cannot list models: HTTP {response.status_code}", response.status_code, response.text[:500])
        data = response.json().get("data") or []
        return [str(item.get("id")) for item in data if isinstance(item, dict)]


def _parse_sse_line(line: str) -> dict[str, Any] | str | None:
    line = line.strip()
    if not line or not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if data == "[DONE]":
        return "[DONE]"
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        log.debug("skipping malformed SSE chunk: %s", data[:200])
        return None
    return parsed if isinstance(parsed, dict) else None


def _merge_chunk(
    chunk: dict[str, Any],
    result: LLMResult,
    calls: dict[int, dict[str, Any]],
    on_token: Callable[[str], None] | None,
) -> None:
    if chunk.get("usage"):
        result.usage = chunk["usage"]
    if chunk.get("model"):
        result.model = str(chunk["model"])
    choices = chunk.get("choices") or []
    if not choices:
        return
    choice = choices[0]
    if choice.get("finish_reason"):
        result.finish_reason = str(choice["finish_reason"])
    delta = choice.get("delta") or {}
    content = delta.get("content")
    if content:
        result.content += content
        if on_token is not None:
            on_token(content)
    for fragment in delta.get("tool_calls") or []:
        index = int(fragment.get("index") or 0)
        entry = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if fragment.get("id"):
            entry["id"] = fragment["id"]
        function = fragment.get("function") or {}
        if function.get("name"):
            entry["name"] = function["name"]
        if function.get("arguments"):
            entry["arguments"] += function["arguments"]
