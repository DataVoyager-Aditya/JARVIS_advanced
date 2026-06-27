"""
Phase 5 — JARVIS's emotional intelligence (free, local, fast).

  analyze(user_text)   -> reads the boss's mood, updates JARVIS's smoothed state, returns a
                          snapshot {register,label,warmth,play,urgency,focus,humor} for the HUD.
  mood_block()         -> the dynamic system-prompt block ("calibrate your tone to THIS …").
  temperature()        -> per-register sampling temperature for the reply.
  prosody()            -> {register, rate, pitch} for the TTS to colour the voice.
  note_reply(text)     -> remember JARVIS's last line (so he doesn't recycle a quip).
  note_user_turn(text) -> count laughter as a "humor hit" (what lands with him).

All synchronous + cheap (one tiny local model + regex). Callers in the async path wrap analyze()
in a thread. Degrades to heuristics if the model can't load — never crashes a turn.
"""

from __future__ import annotations

import logging
import re

from .detector import get_detector, EmotionRead
from .state import get_mood

logger = logging.getLogger("jarvis.emotion")

_LAUGH_HIT = re.compile(r"(\bl+o+l+\b|\blma+o+\b|\brofl\b|\bha(ha)+\b|😂|🤣|😆|that'?s (funny|hilarious)|"
                        r"you'?re funny|good one|made me laugh|nice one|💀)", re.I)


def analyze(user_text: str, voice_emotion: dict | None = None) -> dict:
    """Read the boss's mood (words + optional VOICE TONE) and fold it into JARVIS's state.
    `voice_emotion` is {emotion, intensity, scores} from the speech-emotion model — it catches
    feeling the words hide ('fine' said angrily). Returns the HUD snapshot."""
    read: EmotionRead = get_detector().read(user_text)
    read = _fuse_voice(read, voice_emotion)
    return get_mood().update(read)


# Voice-tone emotion -> the register it should push toward (when the words don't already show it).
_VOICE_TO_REGISTER = {"angry": "frustrated", "anger": "frustrated", "sad": "vulnerable",
                      "sadness": "vulnerable", "fear": "vulnerable", "fearful": "vulnerable",
                      "happy": "playful", "happiness": "playful", "joy": "playful"}
# Registers the words already chose that VOICE must not override (safety / explicit intent).
_VOICE_KEEP = {"vulnerable", "urgent", "frustrated", "sarcastic"}


def _fuse_voice(read: EmotionRead, voice: dict | None) -> EmotionRead:
    if not voice:
        return read
    emo = (voice.get("emotion") or "").lower()
    strength = float(voice.get("intensity") or voice.get("score") or 0.0)
    target = _VOICE_TO_REGISTER.get(emo)
    # Let TONE override a 'neutral'/'playful' text read when it's reasonably sure. This is what
    # catches a flat "i'm fine" that *sounds* upset, without hijacking clear text intent.
    if target and strength >= 0.5 and read.register not in _VOICE_KEEP:
        if target != read.register:
            read.register = target
            read.signals = list(read.signals) + [f"voice:{emo}"]
            read.intensity = max(read.intensity, strength)
    return read


def mood_block() -> str:
    return get_mood().prompt_block()


def temperature() -> float:
    return get_mood().temperature()


def prosody() -> dict:
    return get_mood().prosody()


def snapshot() -> dict:
    return get_mood().snapshot()


def note_reply(text: str) -> None:
    get_mood().note_reply(text)


def note_user_turn(text: str) -> bool:
    """If the boss laughed/appreciated the last line, count it as a humor hit. Returns True if so."""
    if _LAUGH_HIT.search(user_text := (text or "")):
        get_mood().note_humor_hit()
        logger.debug("humor hit (+1): %r", user_text[:50])
        return True
    return False


def warm() -> None:
    get_detector().warm()


__all__ = ["analyze", "mood_block", "temperature", "prosody", "snapshot",
           "note_reply", "note_user_turn", "warm"]
