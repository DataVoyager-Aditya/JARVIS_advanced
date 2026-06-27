"""
Local sentence embeddings — free, offline, no key, no rate limit.

JARVIS's memory must NEVER break or cost money (project rule), so the episodic vector
store runs on a local SentenceTransformer (`all-MiniLM-L6-v2`, 384-dim) rather than a
paid/rotated embedding API. The model is ~80 MB, downloaded once from HuggingFace, then
fully offline forever.

If the model cannot load (e.g. first run with no internet), `available()` returns False
and the episodic store transparently falls back to lexical search — memory degrades, it
never hard-fails.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

logger = logging.getLogger("jarvis.memory.embed")

EMBED_MODEL_NAME = "all-MiniLM-L6-v2"
EMBED_DIM = 384


class Embedder:
    """Lazy, thread-safe singleton wrapper around a local SentenceTransformer."""

    def __init__(self, model_name: str = EMBED_MODEL_NAME) -> None:
        self.model_name = model_name
        self._model = None
        self._retry_after = 0.0   # a failed load is RETRYABLE (a startup import race is transient)
        self._lock = threading.Lock()
        # encode() can be called from multiple worker threads at once (e.g. two concurrent deep-research
        # sweeps via asyncio.to_thread, plus the memory path). A SentenceTransformer isn't guaranteed
        # safe under truly concurrent forward passes, so serialize encodes. Inference is fast (ms), so
        # this adds negligible latency while removing the race.
        self._encode_lock = threading.Lock()

    def _ensure(self) -> bool:
        if self._model is not None:
            return True
        import time
        if time.time() < self._retry_after:   # failed recently — back off, but DO retry later
            return False
        with self._lock:
            if self._model is not None:
                return True
            if time.time() < self._retry_after:
                return False
            try:
                import os
                from sentence_transformers import SentenceTransformer
                logger.info("loading local embedding model '%s'...", self.model_name)
                # Prefer the local cache (fast, silent, offline). Only the very first run
                # downloads; after that we skip the HuggingFace metadata round-trips.
                prev = os.environ.get("HF_HUB_OFFLINE")
                os.environ["HF_HUB_OFFLINE"] = "1"
                try:
                    self._model = SentenceTransformer(self.model_name)
                except Exception:  # not cached yet -> allow a one-time download
                    if prev is None:
                        os.environ.pop("HF_HUB_OFFLINE", None)
                    else:
                        os.environ["HF_HUB_OFFLINE"] = prev
                    self._model = SentenceTransformer(self.model_name)
                else:
                    if prev is None:
                        os.environ.pop("HF_HUB_OFFLINE", None)
                    else:
                        os.environ["HF_HUB_OFFLINE"] = prev
                logger.info("embedding model ready (dim=%d)", EMBED_DIM)
                return True
            except Exception as e:  # noqa: BLE001
                self._retry_after = time.time() + 30   # transient (e.g. import race) — retry in 30s
                logger.warning("embedding model not ready (%s) — lexical fallback for now, will retry", e)
                return False

    def available(self) -> bool:
        return self._ensure()

    def encode(self, texts: list[str]) -> np.ndarray:
        """Return L2-normalized float32 embeddings, shape (n, EMBED_DIM). Cosine similarity
        is then just an inner product (used with a FAISS inner-product index)."""
        if not self._ensure():
            raise RuntimeError("embedding model not available")
        with self._encode_lock:
            vecs = self._model.encode(texts, normalize_embeddings=True,
                                      convert_to_numpy=True, show_progress_bar=False)
        return np.asarray(vecs, dtype=np.float32).reshape(len(texts), -1)

    def encode_one(self, text: str) -> np.ndarray:
        return self.encode([text])[0]


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder
