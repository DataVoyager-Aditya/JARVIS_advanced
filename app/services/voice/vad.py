"""
Voice activity detection (silero-vad).

Two jobs:
  1. End-of-speech: while recording an utterance, know when the user has stopped talking.
  2. Barge-in: while JARVIS is speaking, detect that the user started talking so we can
     cut him off (interrupt) within a couple hundred ms.

silero expects 16 kHz mono float32 in fixed 512-sample frames (~32 ms). Returns a speech
probability per frame.
"""

from __future__ import annotations

import logging

import numpy as np

from config import SAMPLE_RATE

logger = logging.getLogger("jarvis.vad")

FRAME_SAMPLES = 512                       # silero's required frame size at 16 kHz
FRAME_MS = FRAME_SAMPLES / SAMPLE_RATE * 1000


class SileroVAD:
    def __init__(self, threshold: float = 0.5):
        import torch
        from silero_vad import load_silero_vad
        self._torch = torch
        self.model = load_silero_vad()
        self.threshold = threshold
        logger.info("Silero VAD loaded (threshold=%.2f, frame=%dms)", threshold, int(FRAME_MS))

    def reset(self) -> None:
        # Clear the model's internal RNN state between utterances.
        if hasattr(self.model, "reset_states"):
            self.model.reset_states()

    def speech_prob(self, frame: np.ndarray) -> float:
        """frame: float32 mono, exactly FRAME_SAMPLES long (-1..1)."""
        if frame.dtype != np.float32:
            frame = frame.astype(np.float32)
        if len(frame) != FRAME_SAMPLES:
            # pad/truncate to required size
            buf = np.zeros(FRAME_SAMPLES, dtype=np.float32)
            buf[: min(len(frame), FRAME_SAMPLES)] = frame[:FRAME_SAMPLES]
            frame = buf
        t = self._torch.from_numpy(frame)
        with self._torch.no_grad():
            return float(self.model(t, SAMPLE_RATE).item())

    def is_speech(self, frame: np.ndarray) -> bool:
        return self.speech_prob(frame) >= self.threshold


_singleton: SileroVAD | None = None


def get_vad(threshold: float = 0.5) -> SileroVAD:
    global _singleton
    if _singleton is None:
        _singleton = SileroVAD(threshold=threshold)
    return _singleton
