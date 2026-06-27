"""
Speech-to-text.

Primary: cloud STT via the KeyRotator — Groq Whisper (`whisper-large-v3-turbo`) then
Deepgram — free, fast, accurate, quota-aware.
Fallback: local faster-whisper (CPU, offline) — used when every cloud STT key is rate-
limited or the network is down. JARVIS always has a working ear.

Accepts 16 kHz mono PCM (int16 numpy) or raw WAV bytes.
"""

from __future__ import annotations

import io
import logging
import wave

import numpy as np

from config import FASTER_WHISPER_MODEL, SAMPLE_RATE
from app.services.llm.key_rotator import get_rotator

logger = logging.getLogger("jarvis.stt")


def pcm_to_wav_bytes(pcm: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """int16 mono PCM -> in-memory WAV bytes."""
    if pcm.dtype != np.int16:
        pcm = (np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16) if pcm.dtype.kind == "f" else pcm.astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class STTService:
    def __init__(self) -> None:
        self.rotator = get_rotator()
        self._local = None  # lazy faster-whisper

    # -- local fallback (lazy load — model download on first use) --
    def _local_model(self):
        if self._local is None:
            from faster_whisper import WhisperModel
            logger.info("Loading local faster-whisper '%s' (offline fallback)...", FASTER_WHISPER_MODEL)
            self._local = WhisperModel(FASTER_WHISPER_MODEL, device="cpu", compute_type="int8")
        return self._local

    def _transcribe_local(self, wav_bytes: bytes) -> str:
        model = self._local_model()
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            frames = w.readframes(w.getnframes())
            sr = w.getframerate()
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if sr != 16000:
            logger.debug("local STT got %d Hz audio", sr)
        segments, _ = model.transcribe(audio, language="en", beam_size=1)
        return " ".join(s.text for s in segments).strip()

    # ------------------------------------------------------------------ #
    def transcribe(self, audio: bytes | np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        """Transcribe WAV bytes or int16 PCM. Cloud rotation primary, local fallback."""
        wav_bytes = audio if isinstance(audio, (bytes, bytearray)) else pcm_to_wav_bytes(audio, sample_rate)

        if self.rotator.stt_providers:
            try:
                return self.rotator.transcribe(wav_bytes)
            except Exception as e:  # noqa: BLE001
                logger.warning("Cloud STT exhausted (%s) — falling back to local Whisper", e)

        return self._transcribe_local(wav_bytes)


_singleton: STTService | None = None


def get_stt() -> STTService:
    global _singleton
    if _singleton is None:
        _singleton = STTService()
    return _singleton
