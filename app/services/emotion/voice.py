"""
Phase 5 — VOICE-TONE emotion (Speech Emotion Recognition).

Reads emotion from HOW the boss speaks, not what he says — so "fine" said flatly/angrily reads as
upset, the way a human hears it. Uses the SAME 16 kHz audio already captured for speech-to-text.

MODEL: `audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim` (free, local). Crucially it's
trained on MSP-Podcast — REAL, natural conversational speech — so it generalises to everyday
talking instead of over-predicting "neutral" the way the acted-corpus (IEMOCAP/RAVDESS) models do.
It outputs the dimensional axes arousal / dominance / valence, which we map to angry / sad / happy /
neutral. (A lighter categorical fallback model is used if this one can't load.)

SPEED: runs IN PARALLEL with the cloud STT (both only need the audio), ~0.5-1.5 s on CPU — under
the STT round-trip, so ~0 added latency. Lazy-loaded + warmed at startup; degrades to None if it
can't load. Never crashes.
"""

from __future__ import annotations

import io
import logging
import os
import threading
import time
import wave

import numpy as np

logger = logging.getLogger("jarvis.emotion.voice")

_MSP_MODEL = os.getenv("VOICE_EMOTION_MODEL", "audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim")
_FALLBACK_MODEL = "superb/wav2vec2-base-superb-er"
# superb categorical labels (lowercase short forms) -> our words
_LABEL = {"neu": "neutral", "hap": "happy", "ang": "angry", "sad": "sad",
          "neutral": "neutral", "happy": "happy", "angry": "angry"}


def _build_msp_model():
    """The audeering MSP-dim model uses a custom regression head (arousal/dominance/valence)."""
    import torch
    import torch.nn as nn
    from transformers import Wav2Vec2Processor
    from transformers.models.wav2vec2.modeling_wav2vec2 import Wav2Vec2Model, Wav2Vec2PreTrainedModel

    class _RegressionHead(nn.Module):
        def __init__(self, config):
            super().__init__()
            self.dense = nn.Linear(config.hidden_size, config.hidden_size)
            self.dropout = nn.Dropout(config.final_dropout)
            self.out_proj = nn.Linear(config.hidden_size, config.num_labels)

        def forward(self, features):
            x = self.dropout(features)
            x = torch.tanh(self.dense(x))
            x = self.dropout(x)
            return self.out_proj(x)

    class _EmotionModel(Wav2Vec2PreTrainedModel):
        # transformers 5.x expects these on every PreTrainedModel; the original audeering class
        # predates that, so declare them (no tied/missing weights in this head).
        all_tied_weights_keys: dict = {}
        _tied_weights_keys: list = []

        def __init__(self, config):
            super().__init__(config)
            self.wav2vec2 = Wav2Vec2Model(config)
            self.classifier = _RegressionHead(config)
            self.init_weights()

        def forward(self, input_values):
            hidden = self.wav2vec2(input_values)[0]
            hidden = torch.mean(hidden, dim=1)
            return self.classifier(hidden)        # [arousal, dominance, valence]

    processor = Wav2Vec2Processor.from_pretrained(_MSP_MODEL)
    model = _EmotionModel.from_pretrained(_MSP_MODEL).eval()
    return processor, model


def _dims_to_emotion(arousal: float, valence: float) -> tuple[str, float]:
    """Map arousal/valence (0-1, .5 = neutral) to a categorical tone + intensity."""
    # intensity = how far from the neutral centre (more displacement = stronger feeling)
    intensity = min(1.0, max(abs(valence - 0.5), abs(arousal - 0.5)) * 2.2)
    if valence < 0.46 and arousal >= 0.52:
        return "angry", intensity              # negative + activated (frustrated/annoyed)
    if valence < 0.46:
        return "sad", intensity                # negative + calm (down/tired/vulnerable)
    if valence > 0.58 and arousal >= 0.5:
        return "happy", intensity              # positive + activated (upbeat/excited)
    return "neutral", intensity


class VoiceEmotion:
    def __init__(self) -> None:
        self._kind = None            # "msp" | "pipe" once loaded
        self._proc = self._model = self._pipe = None
        self._retry_after = 0.0
        self._lock = threading.Lock()

    def _ensure(self):
        if self._kind is not None:
            return True
        if time.time() < self._retry_after:
            return False
        with self._lock:
            if self._kind is not None:
                return True
            try:
                self._proc, self._model = _build_msp_model()
                self._kind = "msp"
                logger.info("voice-emotion model loaded (audeering MSP-dim, natural speech)")
                return True
            except Exception as e:  # noqa: BLE001
                logger.warning("MSP voice model unavailable (%s) — trying lighter fallback", type(e).__name__)
            try:
                from transformers import pipeline
                self._pipe = pipeline("audio-classification", model=_FALLBACK_MODEL, top_k=None)
                self._kind = "pipe"
                logger.info("voice-emotion model loaded (wav2vec2 SER fallback)")
                return True
            except Exception as e:  # noqa: BLE001
                self._retry_after = time.time() + 30
                logger.warning("voice-emotion model not ready (%s) — text emotion for now, will retry",
                               type(e).__name__)
                return False

    def warm(self) -> None:
        try:
            self._ensure()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _to_array(audio, sr: int) -> tuple[np.ndarray, int]:
        if isinstance(audio, np.ndarray):
            a = audio.astype(np.float32)
            if a.max() > 1.5:
                a = a / 32768.0
            return a, sr
        with wave.open(io.BytesIO(audio), "rb") as w:
            sr = w.getframerate()
            frames = w.readframes(w.getnframes())
        a = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        return a, sr

    def analyze(self, audio, sr: int = 16000) -> dict | None:
        """Return {emotion, intensity, scores} or None if SER is unavailable/too short."""
        if not self._ensure():
            return None
        try:
            arr, rate = self._to_array(audio, sr)
            if arr.size < rate * 0.4:            # < 0.4 s — too short to read tone
                return None
            if self._kind == "msp":
                import torch
                inputs = self._proc(arr, sampling_rate=rate, return_tensors="pt")
                with torch.no_grad():
                    out = self._model(inputs["input_values"])[0].numpy()
                arousal, dominance, valence = float(out[0]), float(out[1]), float(out[2])
                emo, intensity = _dims_to_emotion(arousal, valence)
                return {"emotion": emo, "intensity": round(intensity, 2),
                        "scores": {"arousal": round(arousal, 2), "valence": round(valence, 2),
                                   "dominance": round(dominance, 2)}}
            # categorical fallback
            out = self._pipe({"raw": arr, "sampling_rate": rate})
            scores = {_LABEL.get(r["label"].lower(), r["label"].lower()): float(r["score"]) for r in out}
            top = max(scores, key=scores.get)
            return {"emotion": top, "intensity": round(scores[top], 2), "scores": scores}
        except Exception as e:  # noqa: BLE001
            logger.debug("voice-emotion analyze failed: %s", e)
            return None


_ve: VoiceEmotion | None = None
_lock = threading.Lock()


def get_voice_emotion() -> VoiceEmotion:
    global _ve
    if _ve is None:
        with _lock:
            if _ve is None:
                _ve = VoiceEmotion()
    return _ve
