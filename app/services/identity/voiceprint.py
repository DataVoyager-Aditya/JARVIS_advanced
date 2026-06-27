"""
Voice biometrics (Phase 11) — resemblyzer, free & fully local.

A `VoiceEncoder` (≈40 MB, bundled weights — no download, no key, runs on CPU) turns any 1.2 s+
of speech into a 256-dim L2-normalised voiceprint. Cosine similarity (a plain dot product, since
the vectors are unit-norm) against the enrolled prints decides whose voice it is.

The encoder is heavy to construct, so it's built once, lazily, behind a lock — the first call
pays ~1 s, every call after is ~30 ms for a short clip.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from config import IDENTITY_MIN_VOICE_S

logger = logging.getLogger("jarvis.identity.voice")

_encoder = None
_lock = threading.Lock()
DIM = 256


def _get_encoder():
    global _encoder
    if _encoder is None:
        with _lock:
            if _encoder is None:
                from resemblyzer import VoiceEncoder
                logger.info("loading voice encoder (resemblyzer) …")
                _encoder = VoiceEncoder(verbose=False)
    return _encoder


def _to_float_mono(samples: np.ndarray) -> np.ndarray:
    a = np.asarray(samples)
    if a.dtype == np.int16:
        a = a.astype(np.float32) / 32768.0
    else:
        a = a.astype(np.float32)
    if a.ndim > 1:                     # stereo -> mono
        a = a.mean(axis=1)
    return a


def embed(samples: np.ndarray, sr: int = 16000) -> np.ndarray | None:
    """Voiceprint for a PCM clip (int16 or float). Returns a 256-d unit vector, or None when
    there isn't enough actual speech after silence-trimming to be reliable."""
    try:
        from resemblyzer import preprocess_wav
        wav = preprocess_wav(_to_float_mono(samples), source_sr=sr)
        if wav is None or len(wav) < int(IDENTITY_MIN_VOICE_S * 16000):
            return None                # too little voiced speech — caller treats as "unsure"
        emb = _get_encoder().embed_utterance(wav)
        return np.asarray(emb, dtype=np.float32)
    except Exception as e:  # noqa: BLE001
        logger.warning("voiceprint embed failed: %s", e)
        return None


def embed_wav_bytes(wav_bytes: bytes) -> np.ndarray | None:
    """Voiceprint from a WAV file's bytes (e.g. an uploaded recording)."""
    try:
        import io
        import soundfile as sf
        data, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32", always_2d=False)
        return embed(data, sr)
    except Exception as e:  # noqa: BLE001
        logger.warning("voiceprint embed_wav_bytes failed: %s", e)
        return None


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two voiceprints (already unit-norm → dot product). Range ~[-1, 1]."""
    if a is None or b is None:
        return 0.0
    a = np.asarray(a, dtype=np.float32); b = np.asarray(b, dtype=np.float32)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def average(prints: list[np.ndarray]) -> np.ndarray:
    """Combine several enrolment clips into one centroid voiceprint (re-normalised)."""
    m = np.mean(np.stack([np.asarray(p, dtype=np.float32) for p in prints]), axis=0)
    n = np.linalg.norm(m)
    return (m / n).astype(np.float32) if n else m.astype(np.float32)
