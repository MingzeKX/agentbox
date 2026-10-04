"""Embedding backends for tool retrieval.

Both supported backends emit **1024** dimensional vectors, which matches the
``vector(1024)`` column in the registry.  Switching backend therefore never
requires a schema migration.

* ``local``     - BAAI/bge-m3 through sentence-transformers (offline, needs the
                  optional extra: ``pip install -e ".[local-embed]"``)
* ``dashscope`` - Alibaba text-embedding-v4 through the OpenAI compatible API

If the configured backend cannot be initialised the factory returns
:class:`UnavailableEmbedder`; retrieval then degrades to the keyword half of the
hybrid search instead of failing outright.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import httpx

from agent.config import settings

log = logging.getLogger(__name__)


class EmbeddingUnavailable(RuntimeError):
    pass


@runtime_checkable
class Embedder(Protocol):
    name: str
    dim: int
    available: bool

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_one(self, text: str) -> list[float]: ...


class UnavailableEmbedder:
    name = "unavailable"
    dim = settings.embedding_dim
    available = False

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingUnavailable(self.reason)

    async def embed_one(self, text: str) -> list[float]:
        raise EmbeddingUnavailable(self.reason)


class LocalBGEEmbedder:
    """sentence-transformers backend.  The model is loaded lazily and cached."""

    def __init__(self, model_name: str, dim: int) -> None:
        self.name = f"local:{model_name}"
        self.dim = dim
        self._model_name = model_name
        self._model = None
        self._lock = asyncio.Lock()
        self.available = True

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # heavy, imported on demand

            log.info("loading embedding model %s", self._model_name)
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(
            list(texts),
            batch_size=settings.embedding_batch,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(x) for x in vec] for vec in vectors]

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        async with self._lock:
            out = await asyncio.to_thread(self._encode, texts)
        for vec in out:
            if len(vec) != self.dim:
                raise EmbeddingUnavailable(
                    f"model {self._model_name} returned {len(vec)} dims, registry expects {self.dim}"
                )
        return out

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]


class DashScopeEmbedder:
    """OpenAI compatible embedding endpoint (Alibaba DashScope by default)."""

    def __init__(self, api_key: str, model: str, base_url: str, dim: int) -> None:
        self.name = f"dashscope:{model}"
        self.dim = dim
        self._api_key = api_key
        self._model = model
        self._url = base_url.rstrip("/") + "/embeddings"
        self.available = bool(api_key)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.available:
            raise EmbeddingUnavailable("AGENT_DASHSCOPE_API_KEY is not set")
        payload = {
            "model": self._model,
            "input": list(texts),
            "dimensions": self.dim,
            "encoding_format": "float",
        }
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(self._url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise EmbeddingUnavailable(f"dashscope embeddings HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            rows = sorted(data["data"], key=lambda item: item.get("index", 0))
            vectors = [[float(x) for x in row["embedding"]] for row in rows]
        except (KeyError, TypeError) as exc:
            raise EmbeddingUnavailable(f"unexpected embeddings response: {str(data)[:300]}") from exc
        for vec in vectors:
            if len(vec) != self.dim:
                raise EmbeddingUnavailable(f"endpoint returned {len(vec)} dims, registry expects {self.dim}")
        return vectors

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]


_embedder: Embedder | None = None


def build_embedder() -> Embedder:
    backend = settings.embedding_backend
    if backend == "local":
        try:
            import sentence_transformers  # noqa: F401
        except Exception as exc:  # pragma: no cover - environment dependent
            return UnavailableEmbedder(
                f"sentence-transformers is not installed ({exc}); run pip install -e '.[local-embed]' "
                "or set AGENT_EMBEDDING_BACKEND=dashscope"
            )
        return LocalBGEEmbedder(settings.embedding_model, settings.embedding_dim)
    if backend == "dashscope":
        return DashScopeEmbedder(
            settings.dashscope_api_key,
            settings.dashscope_embedding_model,
            settings.dashscope_base_url,
            settings.embedding_dim,
        )
    return UnavailableEmbedder(f"unknown embedding backend {backend!r}")


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = build_embedder()
        if not _embedder.available:
            reason = getattr(_embedder, "reason", "not available")
            log.warning("embedding backend unavailable (%s); tool search falls back to keyword matching", reason)
    return _embedder


def set_embedder(embedder: Embedder | None) -> None:
    """Test hook / dependency injection."""
    global _embedder
    _embedder = embedder
