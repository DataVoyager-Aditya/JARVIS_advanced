"""
Phase 5 — situational awareness: read the boss's mood from his message.

LOCAL + FREE + FAST. Two signals, combined into a single `register` that drives JARVIS's tone:
  1. A small local emotion classifier (`j-hartmann/emotion-english-distilroberta-base`, ~330 MB,
     CPU, no key, no rate limit) → anger/joy/sadness/fear/… probabilities.
  2. Cheap text heuristics for the things a sentiment model misses — laughter/banter, real
     urgency, sarcasm cues, venting, vulnerability, showing-off.

Deliberately NO per-turn LLM call (that reintroduces latency + rate pressure we just fought).
If the model can't load (offline / low memory), detection degrades to heuristics-only — still
useful, never a crash.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger("jarvis.emotion")

# Registers, in PRIORITY order (first match wins when several fire). Safety/empathy first:
# never crack a joke when he's vulnerable or it's urgent.
VULNERABLE = "vulnerable"
URGENT = "urgent"
FRUSTRATED = "frustrated"
SARCASTIC = "sarcastic"
PLAYFUL = "playful"
SHOWING_OFF = "showing_off"
NEUTRAL = "neutral"

_LAUGH = re.compile(r"(\bl+o+l+\b|\blma+o+\b|\brofl\b|\bha(ha)+\b|\bhe(he)+\b|\blmfao\b|😂|🤣|😆|😄|😁)", re.I)
_JOKING = re.compile(r"\b(jk|just kidding|kidding|i'?m joking|messing with you|on god|fr fr|deadass)\b", re.I)
_URGENT = re.compile(r"\b(asap|urgent(ly)?|right now|immediately|hurry|quick(ly)?|emergency|"
                     r"now now|need it now|on it now|fast)\b", re.I)
_VULN = re.compile(
    r"\b(i feel|i'?m feeling|i'?m so|i am so|so tired|exhausted|burn(ed|t) out|"
    r"can'?t do this|can'?t take|overwhelmed|stressed|anxious|depress(ed|ing)|sad|unhappy|miserable|"
    r"lonely|alone|isolated|lost|empty|numb|hopeless|worthless|useless|"
    r"failure|i('?m| am)? (a )?failure|i('?ve| have)? failed|i fail|not good enough|never good enough|"
    r"not enough|don'?t matter|no one (cares|likes|loves|believes)|nobody (cares|likes|loves|gets)|"
    r"everyone (thinks|hates|left)|hate myself|hate my life|disappoint(ed|ment)|let (everyone|them|you) down|"
    r"messed up|screwed up|i suck|doubt myself|imposter|what'?s the point|"
    r"giving up|wanna give up|want to give up|i'?m done|breaking down|cried|crying|tears)\b", re.I)
_FRUSTRATED = re.compile(r"\b(ugh+|fml|wtf|wth|hate this|so annoying|annoyed|frustrat(ed|ing)|"
                         r"pissed|fed up|so done|fucking|bullshit|nonsense|stupid|broken again|"
                         r"why (the )?(hell|fuck)|come on man|are you kidding)\b", re.I)
_SARCASM = re.compile(r"\b(oh (great|wonderful|fantastic|nice|perfect|lovely|brilliant)|yeah right|"
                      r"sure(,| ) jan|wow,? thanks|how lovely|just great|exactly what i needed|"
                      r"love that for me|can'?t wait)\b", re.I)
_SHOWOFF = re.compile(r"\b(i (just |finally )?(built|made|won|got|finished|shipped|launched|aced|"
                      r"nailed|crushed|cracked|topped|ranked)|guess what i|check this out|look what i|"
                      r"i'?m (kind of |pretty )?proud|killed it|smashed it)\b", re.I)


@dataclass
class EmotionRead:
    register: str = NEUTRAL
    emotion: str = "neutral"        # dominant raw emotion from the model (or heuristic)
    intensity: float = 0.4          # 0-1
    signals: list[str] = field(default_factory=list)   # which cues fired (for debugging/HUD)
    scores: dict = field(default_factory=dict)         # raw model scores (may be empty)


class EmotionDetector:
    def __init__(self) -> None:
        self._pipe = None
        self._retry_after = 0.0      # a failed load is RETRYABLE (a startup import race is
        self._lock = threading.Lock()  # transient) — don't get stuck in heuristics forever

    # ---- the local model (lazy, thread-safe) ------------------------------ #
    def _ensure_model(self):
        if self._pipe is not None:
            return self._pipe
        if time.time() < self._retry_after:
            return None
        with self._lock:
            if self._pipe is not None:
                return self._pipe
            try:
                from transformers import pipeline
                self._pipe = pipeline(
                    "text-classification",
                    model="j-hartmann/emotion-english-distilroberta-base",
                    top_k=None, truncation=True,
                )
                logger.info("emotion model loaded (distilroberta, local)")
            except Exception as e:  # noqa: BLE001
                self._retry_after = time.time() + 30   # transient — try again in 30s
                logger.warning("emotion model not ready (%s) — heuristics for now, will retry",
                               type(e).__name__)
            return self._pipe

    def warm(self) -> None:
        """Preload off the hot path (called at startup like the embedding model)."""
        try:
            self._ensure_model()
        except Exception:  # noqa: BLE001
            pass

    def _model_scores(self, text: str) -> dict:
        pipe = self._ensure_model()
        if pipe is None:
            return {}
        try:
            out = pipe(text[:512])
            rows = out[0] if out and isinstance(out[0], list) else out
            return {r["label"].lower(): float(r["score"]) for r in rows}
        except Exception:  # noqa: BLE001
            return {}

    # ---- the public read -------------------------------------------------- #
    def read(self, text: str) -> EmotionRead:
        text = (text or "").strip()
        if not text:
            return EmotionRead()
        scores = self._model_scores(text)
        emo = max(scores, key=scores.get) if scores else "neutral"
        emo_strength = scores.get(emo, 0.0) if scores else 0.0
        signals: list[str] = []

        laughing = bool(_LAUGH.search(text) or _JOKING.search(text))
        caps_words = sum(1 for w in re.findall(r"[A-Za-z]{3,}", text) if w.isupper())
        bangs = text.count("!")
        # EXPLICIT word cues (high-confidence) vs MODEL-inferred mood (softer). Explicit wins —
        # "asap" means urgent even if the tone sounds anxious to the model.
        text_vuln = bool(_VULN.search(text))
        urgent_kw = bool(_URGENT.search(text)) or (caps_words >= 2 and bangs >= 1)
        text_frust = bool(_FRUSTRATED.search(text))
        sarcastic = bool(_SARCASM.search(text))
        # Lower bars so genuine feeling isn't lost to a cautious model. Sadness/fear (distress) get
        # the lowest bar — better to read someone as down and be gentle than miss it and joke.
        model_vuln = emo in ("sadness", "fear") and emo_strength > 0.42
        model_frust = emo in ("anger", "disgust") and emo_strength > 0.5
        playful = laughing or (emo == "joy" and emo_strength > 0.5)
        showoff = bool(_SHOWOFF.search(text))

        # Priority: genuine vulnerability words > explicit urgency > explicit frustration > sarcasm
        # > model-inferred low mood > playful/showing-off > neutral. Humor never pre-empts distress.
        if text_vuln:
            reg = VULNERABLE; signals.append("vulnerable")
        elif urgent_kw:
            reg = URGENT; signals.append("urgent")
        elif text_frust:
            reg = FRUSTRATED; signals.append("frustrated")
        elif sarcastic:
            reg = SARCASTIC; signals.append("sarcasm")
        elif model_vuln:
            reg = VULNERABLE; signals.append("low-mood(tone)")
        elif model_frust:
            reg = FRUSTRATED; signals.append("anger(tone)")
        elif playful:
            reg = PLAYFUL; signals.append("laughing" if laughing else "upbeat")
        elif showoff:
            reg = SHOWING_OFF; signals.append("showing_off")
        else:
            reg = NEUTRAL
        urgent = urgent_kw
        vuln = text_vuln or model_vuln

        intensity = max(emo_strength, 0.85 if (urgent or vuln) else 0.6 if reg != NEUTRAL else 0.35)
        return EmotionRead(register=reg, emotion=emo, intensity=round(intensity, 2),
                           signals=signals, scores=scores)


_detector: EmotionDetector | None = None
_dlock = threading.Lock()


def get_detector() -> EmotionDetector:
    global _detector
    if _detector is None:
        with _dlock:
            if _detector is None:
                _detector = EmotionDetector()
    return _detector
