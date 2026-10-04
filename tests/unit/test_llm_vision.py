"""Vision (image) input, model switching and the reasoning-effort pass-through.

No network: the LLM client is exercised through ``httpx.MockTransport``, and everything
that can be decided without a request (payload shape, caps, model routing) is tested as a
pure function.  The /llm route is called through its endpoint function so the app does not
need a database, a control plane or a lifespan.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from typing import Any

import httpx
import pytest

from agent.ai import app as app_module
from agent.ai import llm
from agent.ai.app import ChatRequest, ImageIn, _chat_content
from agent.config import settings

# --------------------------------------------------------------------------- helpers


def _solid_png(width: int = 16, height: int = 16, rgb: tuple[int, int, int] = (0, 0, 255)) -> bytes:
    """A valid PNG of one solid colour, stdlib only (zlib + struct, correct CRCs)."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * width
    idat = zlib.compress(row * height, 9)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


class _Recorder:
    """Captures the JSON body of every outgoing /chat/completions request."""

    def __init__(self, answer: str = "蓝色") -> None:
        self.payloads: list[dict[str, Any]] = []
        self.answer = answer

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = self

        def handler(request: httpx.Request) -> httpx.Response:
            recorder.payloads.append(json.loads(request.content.decode("utf-8")))
            body = (
                "data: "
                + json.dumps({"model": "stub", "choices": [{"delta": {"content": recorder.answer}}]})
                + "\n\ndata: [DONE]\n\n"
            )
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

        original = httpx.AsyncClient

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(llm.httpx, "AsyncClient", factory)

    @property
    def payload(self) -> dict[str, Any]:
        assert self.payloads, "no request was sent"
        return self.payloads[-1]


def _client(model: str = "deepseek-chat") -> llm.LLMClient:
    return llm.LLMClient(api_key="test-key", base_url="https://llm.test/v1", model=model)


# ------------------------------------------------------------------ request shape


@pytest.mark.asyncio
async def test_image_request_switches_to_the_vision_model_and_sends_a_data_url(monkeypatch):
    monkeypatch.setattr(settings, "llm_vision_model", "deepseek-flash")
    monkeypatch.setattr(settings, "llm_vision_enabled", True)
    recorder = _Recorder()
    recorder.patch(monkeypatch)
    png = _solid_png()

    client = _client("deepseek-chat")
    messages = [{"role": "user", "content": llm.user_content("这张图是什么颜色？", [(png, "image/png")])}]
    result = await client.chat(messages)

    assert result.content == "蓝色"
    payload = recorder.payload
    # the configured text model does not see images on this gateway -> route to the vision one
    assert payload["model"] == "deepseek-flash"

    parts = payload["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "这张图是什么颜色？"}
    assert parts[1]["type"] == "image_url"
    url = parts[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == png


@pytest.mark.asyncio
async def test_text_only_request_keeps_the_configured_model_and_a_plain_string(monkeypatch):
    monkeypatch.setattr(settings, "llm_vision_enabled", True)
    recorder = _Recorder(answer="hi")
    recorder.patch(monkeypatch)

    client = _client("deepseek-chat")
    await client.chat([{"role": "user", "content": "hello"}])

    payload = recorder.payload
    assert payload["model"] == "deepseek-chat"
    assert payload["messages"][0]["content"] == "hello"
    assert isinstance(payload["messages"][0]["content"], str)


@pytest.mark.asyncio
async def test_a_model_that_already_accepts_images_is_not_switched(monkeypatch):
    monkeypatch.setattr(settings, "llm_vision_model", "deepseek-v4-pro")
    monkeypatch.setattr(settings, "llm_vision_enabled", True)
    recorder = _Recorder()
    recorder.patch(monkeypatch)

    # deepseek-flash is a known vision model, gpt-4o is recognised by the name heuristic:
    # switching either one to the configured vision model would be a downgrade.
    for model in ("deepseek-flash", "gpt-4o"):
        client = _client(model)
        await client.chat(
            [{"role": "user", "content": llm.user_content("see?", [(b"x", "image/png")])}]
        )
        assert recorder.payload["model"] == model


# ------------------------------------------------------------------------ effort


@pytest.mark.asyncio
async def test_effort_is_absent_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "llm_effort", "")
    recorder = _Recorder()
    recorder.patch(monkeypatch)

    await _client().chat([{"role": "user", "content": "hello"}])
    assert "effort" not in recorder.payload


@pytest.mark.asyncio
async def test_effort_is_passed_through_verbatim_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "llm_effort", "max")
    recorder = _Recorder()
    recorder.patch(monkeypatch)

    await _client().chat([{"role": "user", "content": "hello"}])
    assert recorder.payload["effort"] == "max"


# ------------------------------------------------------------------ refusal paths


def test_too_many_images_are_refused(monkeypatch):
    monkeypatch.setattr(settings, "llm_max_images", 2)
    monkeypatch.setattr(settings, "llm_vision_enabled", True)
    images = [(b"abcd", "image/png")] * 3
    with pytest.raises(llm.ImageInputError) as excinfo:
        llm.build_image_parts(images)
    assert excinfo.value.status == 422
    assert "3 images" in str(excinfo.value)
    assert "AGENT_LLM_MAX_IMAGES" in str(excinfo.value)
    # the limit itself is fine
    assert len(llm.build_image_parts(images[:2])) == 2


def test_oversized_image_is_refused_with_413(monkeypatch):
    monkeypatch.setattr(settings, "llm_max_image_bytes", 1024)
    monkeypatch.setattr(settings, "llm_vision_enabled", True)
    with pytest.raises(llm.ImageInputError) as excinfo:
        llm.build_image_parts([(b"x" * 1025, "image/png")])
    assert excinfo.value.status == 413
    assert "1025 bytes" in str(excinfo.value)
    assert "AGENT_LLM_MAX_IMAGE_BYTES" in str(excinfo.value)


def test_disallowed_media_type_is_refused():
    with pytest.raises(llm.ImageInputError) as excinfo:
        llm.image_part(b"II*\x00", "image/tiff")
    assert excinfo.value.status == 422
    assert "image/tiff" in str(excinfo.value)
    assert "image/png" in str(excinfo.value)  # the message lists what is accepted


def test_media_type_aliases_and_empty_payloads():
    assert llm.canonical_media_type("image/jpg") == "image/jpeg"
    assert llm.canonical_media_type("IMAGE/PNG; charset=binary") == "image/png"
    with pytest.raises(llm.ImageInputError):
        llm.image_part(b"", "image/png")


def test_bad_base64_is_refused():
    with pytest.raises(llm.ImageInputError) as excinfo:
        llm.decode_base64_image("not base64!!")
    assert "base64" in str(excinfo.value)
    assert llm.decode_base64_image(base64.b64encode(b"png").decode()) == b"png"


def test_image_input_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "llm_vision_enabled", False)
    with pytest.raises(llm.ImageInputError) as excinfo:
        llm.build_image_parts([(b"x", "image/png")])
    assert excinfo.value.status == 503
    assert "AGENT_LLM_VISION_ENABLED" in str(excinfo.value)


# ------------------------------------------------------------------ /chat payload


def test_chat_content_is_a_plain_string_without_images():
    content = _chat_content(ChatRequest(message="hello"))
    assert content == "hello"


def test_chat_content_is_a_parts_list_with_images(monkeypatch):
    monkeypatch.setattr(settings, "llm_vision_enabled", True)
    png = _solid_png()
    request = ChatRequest(
        message="what colour?",
        images=[ImageIn(media_type="image/png", data_base64=base64.b64encode(png).decode())],
    )
    content = _chat_content(request)
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "what colour?"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_chat_content_rejects_an_oversized_image(monkeypatch):
    monkeypatch.setattr(settings, "llm_max_image_bytes", 16)
    monkeypatch.setattr(settings, "llm_vision_enabled", True)
    request = ChatRequest(
        message="big",
        images=[ImageIn(media_type="image/png", data_base64=base64.b64encode(b"x" * 64).decode())],
    )
    with pytest.raises(llm.ImageInputError) as excinfo:
        _chat_content(request)
    assert excinfo.value.status == 413


def test_content_part_detection():
    assert llm.content_has_image([{"type": "image_url", "image_url": {"url": "x"}}]) is True
    assert llm.content_has_image("plain") is False
    assert llm.messages_have_images([{"role": "user", "content": "plain"}]) is False
    assert llm.messages_have_images([{"role": "user", "content": [{"type": "image_url"}]}]) is True


# --------------------------------------------------------------------- GET /llm


class _FakeLLM:
    """Stands in for app.state.llm: only what the /llm route reads."""

    def __init__(self, ids: list[str] | None = None, error: Exception | None = None) -> None:
        self.model = "deepseek-chat"
        self.base_url = "https://llm.test/v1"
        self.api_key = "test-key"
        self._ids = ids or []
        self._error = error
        self.calls = 0

    async def models(self) -> list[str]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._ids)


def _llm_endpoint():
    app = app_module.create_app()
    route = next(r for r in app.routes if getattr(r, "path", None) == "/llm")
    return app, route.endpoint


@pytest.mark.asyncio
async def test_llm_endpoint_degrades_when_the_provider_call_fails(monkeypatch):
    monkeypatch.setattr(app_module, "_models_cache", (0.0, []))
    monkeypatch.setattr(settings, "llm_vision_model", "deepseek-flash")
    monkeypatch.setattr(settings, "llm_vision_enabled", True)
    monkeypatch.setattr(settings, "llm_effort", "high")

    app, endpoint = _llm_endpoint()
    app.state.llm = _FakeLLM(error=httpx.ConnectError("no route to host"))
    info = await endpoint()

    assert info["available_models"] == list(llm.FALLBACK_MODEL_IDS)
    assert info["model"] == "deepseek-chat"
    assert info["vision_model"] == "deepseek-flash"
    assert info["vision_enabled"] is True
    assert info["effort"] == "high"


@pytest.mark.asyncio
async def test_llm_endpoint_reports_the_provider_models_and_caches_them(monkeypatch):
    monkeypatch.setattr(app_module, "_models_cache", (0.0, []))
    app, endpoint = _llm_endpoint()
    fake = _FakeLLM(ids=["deepseek-flash", "deepseek-v4-pro"])
    app.state.llm = fake

    assert (await endpoint())["available_models"] == ["deepseek-flash", "deepseek-v4-pro"]
    assert (await endpoint())["available_models"] == ["deepseek-flash", "deepseek-v4-pro"]
    assert fake.calls == 1, "the second call must be served from the cache"
    assert 240 <= app_module.MODELS_CACHE_TTL_S <= 360  # ~5 minutes


@pytest.mark.asyncio
async def test_health_llm_block_reports_vision_and_effort_without_network(monkeypatch):
    monkeypatch.setattr(settings, "llm_vision_model", "deepseek-flash")
    monkeypatch.setattr(settings, "llm_effort", "low")

    app = app_module.create_app()
    route = next(r for r in app.routes if getattr(r, "path", None) == "/health")
    app.state.llm = _FakeLLM(error=AssertionError("health must not call the provider"))
    app.state.embedder = type("E", (), {"name": "e", "available": False, "dim": 1024})()

    async def no_db() -> tuple[bool, str]:
        return False, "offline"

    async def no_control() -> dict[str, Any]:
        return {"ok": False}

    monkeypatch.setattr(app_module.registry_db, "ping", no_db)
    app.state.gateway = type("G", (), {"health": staticmethod(no_control)})()
    health = await route.endpoint()

    assert health["llm"]["model"] == "deepseek-chat"
    assert health["llm"]["vision_model"] == "deepseek-flash"
    assert health["llm"]["effort"] == "low"
    assert health["llm"]["vision_enabled"] is True
