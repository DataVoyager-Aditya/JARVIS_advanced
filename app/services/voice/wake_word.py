"""
Wake word detection (openWakeWord).

Phase 1 uses the bundled `hey_jarvis` model — already an exact fit for the name, free,
local ONNX, ~30 MB RAM, <1% CPU. A custom "Wakeup JARVIS" model can be trained later and
dropped into models/ ; point WAKE_WORD_MODEL at the .onnx path and nothing else changes.

Feed it 16 kHz mono int16 frames of 1280 samples (80 ms) — openWakeWord's native frame.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import numpy as np

from config import WAKE_WORD_MODEL, WAKE_WORD_THRESHOLD, WAKE_ENGINE

logger = logging.getLogger("jarvis.wake")

FRAME_SAMPLES = 1280   # openWakeWord native frame @ 16 kHz (80 ms)


@dataclass
class WakeEvent:
    """Emitted when the wake phrase fires. `command` carries a one-breath trailing
    command if the engine heard one (Vosk), else None (greet-then-listen flow)."""
    command: str | None = None


class WakeWord:
    def __init__(self, model: str = WAKE_WORD_MODEL, threshold: float = WAKE_WORD_THRESHOLD):
        from openwakeword.model import Model
        from openwakeword import utils as oww_utils

        # Ensure the bundled ONNX feature models are present (one-time download).
        try:
            oww_utils.download_models()
        except Exception as e:  # noqa: BLE001
            logger.debug("oww download_models: %s", e)

        # Accept either a bundled name ("hey_jarvis") or a path to a custom .onnx/.tflite.
        if os.path.exists(model):
            self.model = Model(wakeword_models=[model], inference_framework="onnx")
            self.key = os.path.splitext(os.path.basename(model))[0]
        else:
            self.model = Model(wakeword_models=[model], inference_framework="onnx")
            self.key = model

        self.threshold = threshold
        logger.info("Wake word ready — '%s' (threshold=%.2f)", self.key, threshold)

    def reset(self) -> None:
        if hasattr(self.model, "reset"):
            self.model.reset()

    def detect(self, frame_int16: np.ndarray) -> float:
        """Return the wake-word confidence for an 80 ms (1280-sample) int16 frame."""
        if frame_int16.dtype != np.int16:
            frame_int16 = frame_int16.astype(np.int16)
        scores = self.model.predict(frame_int16)
        # Take the score for our model key if present, else the max across models.
        if self.key in scores:
            return float(scores[self.key])
        return float(max(scores.values())) if scores else 0.0

    def triggered(self, frame_int16: np.ndarray) -> bool:
        return self.detect(frame_int16) >= self.threshold

    def process(self, frame_int16: np.ndarray) -> WakeEvent | None:
        """Uniform interface with the Vosk engine. oww has no command tail."""
        return WakeEvent(command=None) if self.triggered(frame_int16) else None


_singleton = None


def get_wake_word():
    """Return the configured wake engine (Vosk by default, openWakeWord if WAKE_ENGINE=oww).
    Both expose reset() and process(block_int16) -> WakeEvent | None."""
    global _singleton
    if _singleton is None:
        if WAKE_ENGINE == "vosk":
            from app.services.voice.wake_vosk import VoskWake
            _singleton = VoskWake()
        else:
            _singleton = WakeWord()
    return _singleton
