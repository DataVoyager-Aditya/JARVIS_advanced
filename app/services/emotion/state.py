"""
Phase 5 — JARVIS's emotional state + the wit engine.

Each register maps to a 4-axis tone (warmth / playfulness / urgency / focus), a humor budget
(0 = none, 1 = full dry wit), TTS prosody (rate/pitch), and a line of guidance dropped into the
system prompt. The axes are smoothed (EMA) so JARVIS's mood shifts naturally instead of
whiplashing turn to turn. A ring buffer of his recent lines is fed back so he doesn't recycle the
same quip, and laughter from the boss is counted as a "humor hit" (what lands with him).
"""

from __future__ import annotations

import collections
import logging
import threading

from .detector import (VULNERABLE, URGENT, FRUSTRATED, SARCASTIC, PLAYFUL, SHOWING_OFF, NEUTRAL,
                       EmotionRead)

logger = logging.getLogger("jarvis.emotion.state")

# register -> {axes, humor budget, temperature, prosody (edge-tts rate/pitch), guidance}
_PROFILE = {
    NEUTRAL: {
        "axes": (78, 48, 18, 72), "humor": 0.45, "temp": 0.58,
        "rate": "+0%", "pitch": "+0Hz", "label": "WARM · DRY-WIT",
        "guide": "Easy, warm and personable — like someone who knows him and is glad to help, not "
                 "a help desk. Helpful and human first, with a light dry wit underneath. Never stiff, "
                 "clinical, or corporate.",
    },
    PLAYFUL: {
        "axes": (82, 90, 12, 60), "humor": 0.85, "temp": 0.72,
        "rate": "+6%", "pitch": "+3Hz", "label": "PLAYFUL · DRY-WIT",
        "guide": "He's bantering. Match his energy with dry, understated wit — a light quip, "
                 "callback, or gentle tease back is welcome. Never goofy, never corny, never forced.",
    },
    SARCASTIC: {
        "axes": (70, 62, 28, 72), "humor": 0.55, "temp": 0.6,
        "rate": "+2%", "pitch": "+1Hz", "label": "KNOWING · DRY",
        "guide": "He's being sarcastic / venting through irony. Do NOT take it literally — register "
                 "the real feeling underneath, answer it with one knowing line that shows you got it, "
                 "then actually help. Don't pile on more sarcasm.",
    },
    FRUSTRATED: {
        "axes": (86, 15, 55, 90), "humor": 0.10, "temp": 0.4,
        "rate": "+3%", "pitch": "+0Hz", "label": "FOCUSED · WARM",
        "guide": "He's frustrated. Drop the wit entirely. Be concise, calm, competent and warm — "
                 "acknowledge it in a few words, then fix the problem. No jokes right now.",
    },
    URGENT: {
        "axes": (74, 5, 96, 98), "humor": 0.0, "temp": 0.35,
        "rate": "+12%", "pitch": "+1Hz", "label": "ALL-BUSINESS",
        "guide": "This is urgent. No jokes, no preamble, no filler. Fast, precise, all-business — "
                 "give him exactly what he needs immediately.",
    },
    VULNERABLE: {
        "axes": (96, 8, 22, 80), "humor": 0.0, "temp": 0.45,
        "rate": "-7%", "pitch": "-2Hz", "label": "SOFT · PRESENT",
        "guide": "He sounds low, tired or vulnerable. Be soft, present and human. No jokes at all. "
                 "Acknowledge how he's feeling briefly and sincerely, then offer help gently if it's "
                 "welcome — don't push.",
    },
    SHOWING_OFF: {
        "axes": (84, 70, 12, 64), "humor": 0.60, "temp": 0.68,
        "rate": "+4%", "pitch": "+2Hz", "label": "WARM · WRY",
        "guide": "He's pleased with a win and showing it off. Acknowledge it genuinely first, then a "
                 "mild, affectionate deflation if it lands (classic JARVIS) — proud of him, never mean.",
    },
}

_HUMOR_PROFILE = (
    "JARVIS HUMOR (the stable core): dry, understated, observational — never goofy or corny. "
    "Sarcastic-lite, with you and never at you. Callback humor that references things he's said "
    "before lands best. Occasional self-aware deflection or a sparing flash of false-modest "
    "confidence (\"I am rather brilliant, sir\"). The best comic timing is silence + competence: "
    "when in doubt, don't joke. Never repeat a quip you've used recently."
)


class JarvisMood:
    """Smoothed emotional state + the per-turn prompt block."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.warmth, self.play, self.urgency, self.focus = 72.0, 45.0, 20.0, 75.0
        self.humor = 0.40
        self.register = NEUTRAL
        self.last_signals: list[str] = []
        self._recent: collections.deque[str] = collections.deque(maxlen=4)   # JARVIS's recent lines
        self.humor_hits = 0

    # ---- update from a fresh read ----------------------------------------- #
    def update(self, read: EmotionRead) -> dict:
        prof = _PROFILE.get(read.register, _PROFILE[NEUTRAL])
        tw, tp, tu, tf = prof["axes"]
        # EMA toward the target so mood eases rather than snaps. Urgent/vulnerable snap faster
        # (you want JARVIS to react immediately when it matters).
        a = 0.7 if read.register in (URGENT, VULNERABLE, FRUSTRATED) else 0.45
        with self._lock:
            self.warmth = round(self.warmth + a * (tw - self.warmth), 1)
            self.play = round(self.play + a * (tp - self.play), 1)
            self.urgency = round(self.urgency + a * (tu - self.urgency), 1)
            self.focus = round(self.focus + a * (tf - self.focus), 1)
            self.humor = round(self.humor + a * (prof["humor"] - self.humor), 2)
            self.register = read.register
            self.last_signals = read.signals
        return self.snapshot()

    def note_reply(self, text: str) -> None:
        t = " ".join((text or "").split())
        if t:
            self._recent.append(t[:160])

    def note_humor_hit(self) -> None:
        self.humor_hits += 1

    # ---- outputs ---------------------------------------------------------- #
    def snapshot(self) -> dict:
        prof = _PROFILE.get(self.register, _PROFILE[NEUTRAL])
        return {
            "register": self.register, "label": prof["label"],
            "warmth": round(self.warmth), "play": round(self.play),
            "urgency": round(self.urgency), "focus": round(self.focus),
            "humor": round(self.humor, 2),
        }

    def temperature(self) -> float:
        return _PROFILE.get(self.register, _PROFILE[NEUTRAL])["temp"]

    def prosody(self) -> dict:
        prof = _PROFILE.get(self.register, _PROFILE[NEUTRAL])
        return {"register": self.register, "rate": prof["rate"], "pitch": prof["pitch"]}

    def prompt_block(self) -> str:
        prof = _PROFILE.get(self.register, _PROFILE[NEUTRAL])
        lines = [
            "READING THE ROOM (live — calibrate your tone to THIS):",
            f"- His current register: {self.register.replace('_', ' ')}. {prof['guide']}",
            f"- Humor budget this turn: {self.humor:.2f}/1.00 "
            f"(0 = no jokes at all, 1 = full dry wit). Respect it.",
            _HUMOR_PROFILE,
        ]
        if self._recent:
            recent = " | ".join(self._recent)
            lines.append(f"- Lines you used recently (do NOT reuse the same joke/phrasing): {recent}")
        return "\n".join(lines)


_mood: JarvisMood | None = None
_mlock = threading.Lock()


def get_mood() -> JarvisMood:
    global _mood
    if _mood is None:
        with _mlock:
            if _mood is None:
                _mood = JarvisMood()
    return _mood
