"""
Text-to-speech — JARVIS's voice.

Primary: Edge-TTS (Microsoft, free, unlimited, no key) with `en-GB-RyanNeural` — the
refined British male that gives JARVIS his sound. Always available, never costs money.

Optional upgrade: ElevenLabs (rotated free keys) — used only when JARVIS_TTS_ENGINE=
elevenlabs AND keys exist; on any failure it falls straight back to Edge so the voice
never breaks and never costs anything.

Two output shapes:
  - `synthesize_mp3(text)`  -> MP3 bytes (for the HTTP endpoint / browser playback)
  - `synthesize_pcm(text)`  -> (float32 mono PCM, samplerate) (for local sounddevice playback)
"""

from __future__ import annotations

import asyncio
import io
import logging

import numpy as np

from config import (
    EDGE_TTS_VOICE, EDGE_TTS_RATE, EDGE_TTS_PITCH,
    TTS_ENGINE, ELEVENLABS_API_KEYS, ELEVENLABS_VOICE_ID,
)

logger = logging.getLogger("jarvis.tts")


# --------------------------------------------------------------------------- #
# MP3 -> PCM decode (soundfile bundles libsndfile >= 1.1 which decodes MP3).
# --------------------------------------------------------------------------- #
def decode_mp3_to_pcm(mp3_bytes: bytes) -> tuple[np.ndarray, int]:
    import soundfile as sf
    data, sr = sf.read(io.BytesIO(mp3_bytes), dtype="float32", always_2d=False)
    if data.ndim > 1:                      # downmix to mono
        data = data.mean(axis=1)
    return data.astype(np.float32), int(sr)


# --------------------------------------------------------------------------- #
# Edge-TTS (primary)
# --------------------------------------------------------------------------- #
async def _edge_mp3(text: str, rate: str | None = None, pitch: str | None = None) -> bytes:
    import edge_tts
    comm = edge_tts.Communicate(text, voice=EDGE_TTS_VOICE,
                                rate=rate or EDGE_TTS_RATE, pitch=pitch or EDGE_TTS_PITCH)
    chunks = bytearray()
    async for ev in comm.stream():
        if ev["type"] == "audio":
            chunks.extend(ev["data"])
    if not chunks:
        raise RuntimeError("Edge-TTS returned no audio")
    return bytes(chunks)


# --------------------------------------------------------------------------- #
# ElevenLabs (optional upgrade)
# --------------------------------------------------------------------------- #
def _eleven_mp3(text: str) -> bytes:
    from elevenlabs.client import ElevenLabs
    last_err: Exception | None = None
    for key in ELEVENLABS_API_KEYS:
        try:
            client = ElevenLabs(api_key=key)
            audio = client.text_to_speech.convert(
                voice_id=ELEVENLABS_VOICE_ID or "JBFqnCBsd6RMkjVDRZzb",
                model_id="eleven_turbo_v2_5",
                text=text,
                output_format="mp3_44100_128",
            )
            return b"".join(audio)
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("ElevenLabs key failed (%s) — rotating", type(e).__name__)
    raise RuntimeError(f"All ElevenLabs keys failed: {last_err}")


class TTSService:
    def __init__(self) -> None:
        self.use_eleven = (TTS_ENGINE == "elevenlabs") and bool(ELEVENLABS_API_KEYS)
        logger.info(
            "TTSService ready — engine=%s voice=%s",
            "elevenlabs(+edge fallback)" if self.use_eleven else "edge", EDGE_TTS_VOICE,
        )

    async def synthesize_mp3(self, text: str, rate: str | None = None,
                             pitch: str | None = None) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""
        if self.use_eleven:
            try:
                return await asyncio.to_thread(_eleven_mp3, text)
            except Exception as e:  # noqa: BLE001
                logger.warning("ElevenLabs failed (%s) — falling back to Edge-TTS", e)
        return await _edge_mp3(text, rate, pitch)

    async def synthesize_pcm(self, text: str, rate: str | None = None,
                             pitch: str | None = None) -> tuple[np.ndarray, int]:
        """rate/pitch (e.g. "+6%", "-2Hz") let Phase 5 colour the voice per register — wit a touch
        livelier, distress softer/slower. None = the configured JARVIS default."""
        mp3 = await self.synthesize_mp3(text, rate, pitch)
        if not mp3:
            return np.zeros(0, dtype=np.float32), 24000
        return await asyncio.to_thread(decode_mp3_to_pcm, mp3)


_singleton: TTSService | None = None


def get_tts() -> TTSService:
    global _singleton
    if _singleton is None:
        _singleton = TTSService()
    return _singleton
