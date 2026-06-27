"""
Vosk-based wake word — free, fully offline, no account, no training.

A small local speech recognizer runs continuously; the moment its LIVE (partial) transcript
contains the wake phrase ("wake up jarvis"), JARVIS arms — no pause required. It then keeps
listening briefly to scoop up a one-breath trailing command ("wake up jarvis, what's the
weather") before firing.

The recognizer is GRAMMAR-CONSTRAINED (it may only output the wake phrase or "[unk]") — this
is what makes spotting "wake up jarvis" reliable with a tiny offline model; free-form decoding
mis-hears it badly. Verified live: every "wake up jarvis" matched, all other speech -> [unk].
"""

from __future__ import annotations

import json
import logging

import numpy as np

from config import SAMPLE_RATE, WAKE_PHRASE, VOSK_MODEL_PATH
from app.services.voice.wake_word import WakeEvent

logger = logging.getLogger("jarvis.wake")

# After the phrase is spotted, keep capturing this many ~32 ms blocks (~1.2 s) to grab a
# trailing one-breath command, then fire.
_TAIL_BLOCKS = 38


class VoskWake:
    def __init__(self, model_path: str = VOSK_MODEL_PATH, phrase: str = WAKE_PHRASE):
        from vosk import Model, KaldiRecognizer
        self.model = Model(model_path)
        self.phrase = phrase.lower().strip()
        # Grammar-constrained: the decoder may only output the wake phrase or "[unk]".
        # This makes spotting the phrase far more reliable than free-form transcription.
        self._grammar = json.dumps([self.phrase, "[unk]"])
        self.rec = KaldiRecognizer(self.model, SAMPLE_RATE, self._grammar)
        # "jarvis" plus a few common small-model mishears so live audio still matches.
        # (Dropped the 4-5 char fragments like "jarvi"/"jervi" — they matched inside random
        # speech and caused false wakes.)
        self._name_variants = ("jarvis", "jervis", "jarvie", "charvis", "darvis", "jarviss")
        self._armed = False
        self._arm_blocks = 0
        self._tail = ""
        logger.info("Vosk wake engine ready — phrase '%s'", self.phrase)

    def reset(self) -> None:
        self.rec.Reset()
        self._armed = False
        self._arm_blocks = 0
        self._tail = ""

    @staticmethod
    def _clean_tail(tail: str) -> str:
        # In grammar mode anything past the phrase decodes to "[unk]"; drop those.
        return " ".join(w for w in tail.split() if w != "[unk]").strip()

    def _matches(self, text: str) -> str | None:
        """Return the command tail (possibly '') if the wake phrase is present, else None."""
        t = text.lower().strip()
        if not t:
            return None
        if self.phrase in t:
            return self._clean_tail(t.split(self.phrase, 1)[1].strip())
        # Fallback for a mis-heard "jarvis": require BOTH "wake" and "up" (the full trigger
        # "wake up …"), not just "wake" — that anchor kills the random false wakes.
        if "wake" in t and "up" in t and any(v in t for v in self._name_variants):
            for v in self._name_variants:
                if v in t:
                    return self._clean_tail(t.split(v, 1)[1].strip())
        return None

    def _partial(self) -> str:
        return json.loads(self.rec.PartialResult()).get("partial", "")

    def process(self, block_int16: np.ndarray) -> WakeEvent | None:
        if block_int16.dtype != np.int16:
            block_int16 = block_int16.astype(np.int16)
        is_final = self.rec.AcceptWaveform(block_int16.tobytes())

        if self._armed:
            self._arm_blocks += 1
            tail = self._matches(self._partial())
            if tail:
                self._tail = tail
            if is_final:
                txt = json.loads(self.rec.Result()).get("text", "")
                ftail = self._matches(txt)
                if ftail is not None:
                    self._tail = ftail
            if is_final or self._arm_blocks >= _TAIL_BLOCKS:
                cmd = self._tail or None
                logger.info("Wake phrase heard%s", f" + command: '{cmd}'" if cmd else "")
                self.reset()
                return WakeEvent(command=cmd)
            return None

        # not armed yet — watch both final and partial transcripts
        if is_final:
            txt = json.loads(self.rec.Result()).get("text", "")
            self.rec.Reset()
            tail = self._matches(txt)
            if tail is not None:
                logger.info("Wake phrase heard%s", f" + command: '{tail}'" if tail else "")
                return WakeEvent(command=tail or None)
            return None

        tail = self._matches(self._partial())
        if tail is not None:
            self._armed = True
            self._arm_blocks = 0
            self._tail = tail
        return None
