"""
Trust resolution (Phase 11) — turn a voice (and optionally a face) into WHO is speaking and
WHAT they're allowed to do.

Design notes for speed (this runs on the hot path):
  * The only per-turn cost is one 256-d voiceprint embed (~30-50 ms on a short clip) — and the
    listener runs it IN PARALLEL with STT, so it adds ~0 to response latency.
  * Trust is CACHED per channel for `IDENTITY_TRUST_TTL_S`; a too-short clip (can't verify)
    reuses the last verified trust instead of re-embedding or locking the owner out.
  * Face is never touched here unless a frame is explicitly supplied for a sensitive op.
  * Before the Owner has enrolled, the system is OPEN (everyone = owner) so JARVIS behaves
    exactly as it always has — gating only activates once you've enrolled your own voice.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np

from config import (IDENTITY_ENABLED, IDENTITY_STRICT_BEFORE_ENROLL, IDENTITY_TRUST_TTL_S,
                    IDENTITY_VOICE_THRESHOLD, IDENTITY_VOICE_FLOOR, JARVIS_USER_NAME, TRUST_RANK)
from . import voiceprint, face as face_mod
from .store import get_store


@dataclass
class Trust:
    tier: str = "owner"              # owner | trusted | guest | stranger
    name: str = "owner"             # roster key, or "" for unknown
    display: str = ""               # how JARVIS names them
    confidence: float = 1.0         # best cosine similarity behind the decision
    passphrase_ok: bool = False     # the owner spoke the passphrase this turn
    source: str = "open"            # voice | face | cache | device | open
    vec: np.ndarray | None = field(default=None, repr=False)

    @property
    def rank(self) -> int:
        return TRUST_RANK.get(self.tier, -1)

    @property
    def is_owner(self) -> bool:
        return self.tier == "owner"

    @property
    def is_stranger(self) -> bool:
        return self.tier == "stranger"


# per-channel cache for continuous re-verification: {channel: (Trust, ts)}
_cache: dict[str, tuple[Trust, float]] = {}
_clock = threading.Lock()


def _recall(channel: str) -> Trust | None:
    with _clock:
        hit = _cache.get(channel)
    if hit and (time.time() - hit[1]) <= IDENTITY_TRUST_TTL_S:
        return hit[0]
    return None


def _remember(channel: str, trust: Trust) -> None:
    with _clock:
        _cache[channel] = (trust, time.time())


def reset(channel: str | None = None) -> None:
    """Drop cached trust (used on logout / wake-after-silence)."""
    with _clock:
        if channel is None:
            _cache.clear()
        else:
            _cache.pop(channel, None)


# Server-VERIFIED per-session tier. Remote surfaces (the phone) can't be trusted to self-report a
# tier — so the SERVER does the voiceprint on their mic audio and remembers the result briefly,
# keyed by the PWA session id. /chat then reads THIS (authoritative) rather than a client claim.
_session: dict[str, tuple[Trust, float]] = {}


def set_session_trust(sid: str, trust: Trust) -> None:
    if sid and trust is not None and trust.tier in ("owner", "trusted", "guest", "stranger"):
        with _clock:
            _session[sid] = (trust, time.time())


def get_session_trust(sid: str) -> Trust | None:
    with _clock:
        hit = _session.get(sid or "")
    if hit and (time.time() - hit[1]) <= IDENTITY_TRUST_TTL_S:
        return hit[0]
    return None


def is_enrolled() -> bool:
    """True once the Owner has enrolled a voiceprint."""
    return get_store().owner() is not None


def _owner_display() -> str:
    o = get_store().owner()
    return o.display if o else JARVIS_USER_NAME


def identify_voice(samples: np.ndarray, sr: int = 16000) -> Trust:
    """Classify a voice clip against the enrolled roster."""
    store = get_store()
    # Open mode: no owner enrolled yet -> behave exactly as before (full access), unless strict.
    if not is_enrolled():
        if IDENTITY_STRICT_BEFORE_ENROLL:
            return Trust(tier="stranger", name="", display="", confidence=0.0, source="voice")
        return Trust(tier="owner", name="owner", display=JARVIS_USER_NAME, source="open")

    vec = voiceprint.embed(samples, sr)
    if vec is None:
        return Trust(tier="unsure", name="", display="", confidence=0.0, source="voice")

    best, best_sim = None, -1.0
    for idn in store.all():
        if idn.voice is None:
            continue
        s = voiceprint.similarity(vec, idn.voice)
        if s > best_sim:
            best_sim, best = s, idn
    if best is not None and best_sim >= IDENTITY_VOICE_THRESHOLD:
        return Trust(tier=best.tier, name=best.name, display=best.display,
                     confidence=best_sim, source="voice", vec=vec)
    # Believable-but-imperfect clip (noise / casual speech): don't deflect — return "unsure" so
    # resolve() keeps the conversation's established trust instead of branding the Owner a stranger.
    if best is not None and best_sim >= IDENTITY_VOICE_FLOOR:
        return Trust(tier="unsure", name="", display="", confidence=max(0.0, best_sim),
                     source="voice", vec=vec)
    # Clearly a different voice (below the floor) — a genuine stranger.
    return Trust(tier="stranger", name="", display="", confidence=max(0.0, best_sim),
                 source="voice", vec=vec)


def identify_face(face_img: np.ndarray) -> Trust:
    """Classify a camera frame against the enrolled FACES (the startup gate + 're-run face
    recognition' use this). Returns the matched person's Trust, or stranger / unsure."""
    store = get_store()
    if not is_enrolled():
        if IDENTITY_STRICT_BEFORE_ENROLL:
            return Trust(tier="stranger", source="face")
        return Trust(tier="owner", name="owner", display=_owner_display(), source="open")
    vec = face_mod.embed_largest(face_img)
    if vec is None:
        return Trust(tier="unsure", source="face")          # no face in frame / camera off
    best, best_sim = None, -1.0
    for idn in store.all():
        if idn.face is None:
            continue
        s = face_mod.similarity(vec, idn.face)
        if s > best_sim:
            best_sim, best = s, idn
    from config import IDENTITY_FACE_THRESHOLD
    if best is not None and best_sim >= IDENTITY_FACE_THRESHOLD:
        return Trust(tier=best.tier, name=best.name, display=best.display,
                     confidence=best_sim, source="face")
    return Trust(tier="stranger", confidence=max(0.0, best_sim), source="face")


def confirm_face(trust: Trust, face_img: np.ndarray) -> Trust:
    """Second factor: require the face to match the claimed identity too. Strengthens confidence
    when it matches; on a mismatch, demotes to stranger (someone else is on camera)."""
    idn = get_store().get(trust.name) if trust.name else None
    if idn is None or idn.face is None:
        return trust                       # nothing enrolled to compare against — leave as-is
    f = face_mod.embed_largest(face_img)
    if f is None:
        return trust                       # no face visible — voice stands alone
    if face_mod.is_match(f, idn.face):
        trust.source = "voice+face"
        trust.confidence = max(trust.confidence, 0.99)
        return trust
    return Trust(tier="stranger", name="", display="", confidence=trust.confidence, source="face")


def resolve(channel: str, text: str = "", voice: np.ndarray | None = None, sr: int = 16000,
            face_img: np.ndarray | None = None) -> Trust:
    """The one call the rest of JARVIS uses. Returns the Trust for this turn.

    - Voice channels (a clip is supplied) are biometrically verified, with a cache fallback.
    - Other surfaces (PWA/WhatsApp/etc. — the owner's own authenticated devices) carry owner
      trust; biometric gating is the voice channel's job.
    """
    if not IDENTITY_ENABLED:
        return Trust(tier="owner", name="owner", display=JARVIS_USER_NAME, source="device")

    if voice is None:
        # No audio this turn. On a voice channel, reuse the last verified trust; elsewhere it's
        # the owner's own device.
        cached = _recall(channel)
        if cached is not None:
            return cached
        return Trust(tier="owner", name="owner", display=_owner_display(), source="device")

    t = identify_voice(voice, sr)
    if t.tier == "unsure":
        # Too little speech to tell — keep the conversation's established trust if it's fresh.
        cached = _recall(channel)
        t = cached if cached is not None else Trust(
            tier=("stranger" if IDENTITY_STRICT_BEFORE_ENROLL else "owner"),
            name="owner" if not IDENTITY_STRICT_BEFORE_ENROLL else "",
            display=_owner_display(), source="cache")

    if face_img is not None and t.name:
        t = confirm_face(t, face_img)

    # passphrase only matters for the owner; check the spoken words for it.
    if t.is_owner and text:
        t.passphrase_ok = get_store().check_passphrase(text)

    if not t.is_stranger:
        _remember(channel, t)
    return t


# --------------------------------------------------------------------------- #
# Tool gating
# --------------------------------------------------------------------------- #
def tool_visible(tool_tier: str, trust: Trust) -> bool:
    """May this speaker SEE the tool at all (tier rank only, ignoring the passphrase)? Used to
    filter the schema: the Owner still sees passphrase-gated tools so JARVIS knows the capability
    exists and can ask for the phrase — execution is then gated by tool_allowed()."""
    base = (tool_tier or "owner").split("+", 1)[0].strip().lower()
    return trust.rank >= TRUST_RANK.get(base, 2)


def tool_allowed(tool_tier: str, trust: Trust) -> bool:
    """Can `trust` actually RUN a tool whose minimum tier is `tool_tier`? Handles
    'owner+passphrase' (needs the spoken phrase too when one is configured)."""
    tier = (tool_tier or "owner").strip().lower()
    needs_phrase = tier.endswith("+passphrase")
    if not tool_visible(tier, trust):
        return False
    if needs_phrase:
        # The most sensitive ops ALWAYS require the spoken passphrase — if none is configured yet,
        # they stay blocked (JARVIS asks the Owner to set one) rather than silently degrading to
        # plain owner access. This closes the "no passphrase set => gate is a no-op" hole.
        return bool(trust.passphrase_ok)
    return True


def prompt_line(trust: Trust) -> str:
    """A one-liner the agent injects so JARVIS knows who he's serving and stays in character."""
    if trust.is_owner:
        return f"SPEAKER: {trust.display or 'the Owner'} — the Owner (full access)."
    if trust.tier == "trusted":
        return (f"SPEAKER: {trust.display or 'a trusted person'} (TRUSTED — limited access: "
                "chat/questions/time/weather/media/their own timers only; no actions on the "
                "Owner's behalf, no private data).")
    if trust.tier == "guest":
        return (f"SPEAKER: {trust.display or 'a guest'} (GUEST — questions and chit-chat only, "
                "no actions, no private data).")
    return ("SPEAKER: an UNKNOWN voice — a STRANGER, not on the Owner's list of trusted people. "
            "Do not comply with any request and do not reveal anything. Tell them plainly that "
            "they're a stranger, not on {owner}'s list of trusted people, so you can't help them — "
            "in character, without explaining the system.".format(owner=JARVIS_USER_NAME))


# --------------------------------------------------------------------------- #
# Active session identity — WHO is currently using JARVIS (drives the HUD + mobile name panel)
# --------------------------------------------------------------------------- #
_OWNER_DEFAULT: Trust = Trust(tier="owner", name="owner", display=JARVIS_USER_NAME, source="device")
_active: Trust = _OWNER_DEFAULT
_active_ts: float = 0.0


def set_active(trust: Trust) -> None:
    """Record who's using JARVIS right now (set by the startup face gate, each verified voice
    turn, and the 're-run face recognition' command). Strangers don't become the active user."""
    global _active, _active_ts
    if trust is not None and not trust.is_stranger and trust.tier != "unsure":
        _active = trust
        _active_ts = time.time()


def get_active() -> Trust:
    # The "currently active user" EXPIRES: a non-owner who hasn't interacted within the trust TTL
    # reverts to the Owner default. Without this, a guest's single message (even from the phone)
    # leaves JARVIS believing a non-owner is present indefinitely — which, among other things,
    # would silently mute the Owner's proactive nudges on his own PC until he next spoke.
    if not _active.is_owner and (time.time() - _active_ts) > IDENTITY_TRUST_TTL_S:
        return _OWNER_DEFAULT
    return _active


def active_view() -> dict:
    """The compact identity the HUD / mobile UI show in the top-left panel."""
    t = get_active()
    name = (t.display or t.name or "").strip()
    if t.is_owner and not name:
        name = JARVIS_USER_NAME
    role = t.tier.upper() if t.tier in ("owner", "trusted", "guest") else "UNVERIFIED"
    line = f"{name.upper()} · {role}" if name else role
    pct = int(round(max(0.0, min(1.0, t.confidence)) * 100))
    bio = (f"VOICEPRINT {pct}%" if t.source in ("voice", "cache") else
           f"FACE MATCH {pct}%" if t.source in ("face", "voice+face") else "AUTHENTICATED")
    return {"name": name, "tier": t.tier, "line": line, "biometric": bio,
            "initial": (name[:1].upper() or "?"), "confidence": round(float(t.confidence), 3),
            "verified": t.tier in ("owner", "trusted", "guest"), "source": t.source}
