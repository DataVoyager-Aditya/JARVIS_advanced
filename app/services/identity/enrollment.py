"""
Enrollment (Phase 11) — capture someone's biometrics and file them under a trust tier.

The Owner enrolls himself first (reads a few sentences → averaged voiceprint, optional face).
Then he can add "dear ones" as trusted or guest. Everything is stored encrypted via the store.
"""

from __future__ import annotations

import logging

import numpy as np

from . import voiceprint, face as face_mod
from .store import get_store

logger = logging.getLogger("jarvis.identity.enroll")

VALID_TIERS = {"owner", "trusted", "guest"}


def _embed_clips(clips: list[np.ndarray], sr: int) -> tuple[np.ndarray | None, int]:
    """Average the voiceprints of several enrolment clips. Returns (centroid, usable_count)."""
    vecs = []
    for c in clips or []:
        v = voiceprint.embed(c, sr)
        if v is not None:
            vecs.append(v)
    if not vecs:
        return None, 0
    return voiceprint.average(vecs), len(vecs)


def _embed_faces(face_imgs: list[np.ndarray] | None) -> np.ndarray | None:
    feats = []
    for img in face_imgs or []:
        f = face_mod.embed_largest(img)
        if f is not None:
            feats.append(f)
    return face_mod.average(feats) if feats else None


def enroll(name: str, tier: str, clips: list[np.ndarray], sr: int = 16000,
           face_imgs: list[np.ndarray] | None = None, display: str | None = None) -> dict:
    """Enroll/replace a person. `clips` are raw PCM arrays (int16/float) at `sr`."""
    tier = (tier or "").strip().lower()
    if tier not in VALID_TIERS:
        return {"ok": False, "message": f"Unknown trust tier '{tier}'."}
    if not (name or "").strip():
        return {"ok": False, "message": "I need a name to enrol them under."}

    voice, n = _embed_clips(clips, sr)
    if voice is None:
        return {"ok": False, "message": "I couldn't get a clear enough voice sample — let's try "
                                        "again somewhere quieter, a little longer each line."}
    face = _embed_faces(face_imgs)

    store = get_store()
    if tier == "owner":
        existing = store.owner()
        if existing and existing.name != (name or "").strip().lower():
            store.remove(existing.name)        # only one Owner
    store.add(name=name, tier=tier, voice=voice, face=face, display=display or name, samples=n)
    return {"ok": True, "name": name, "tier": tier, "samples": n, "face": face is not None,
            "message": f"Enrolled {display or name} as {tier}"
                       + (" with a face match too." if face is not None else ".")}


def enroll_owner(clips: list[np.ndarray], sr: int = 16000,
                 face_imgs: list[np.ndarray] | None = None, display: str | None = None) -> dict:
    from config import JARVIS_USER_NAME
    return enroll(name=display or JARVIS_USER_NAME, tier="owner", clips=clips, sr=sr,
                  face_imgs=face_imgs, display=display or JARVIS_USER_NAME)


def remove(name: str) -> dict:
    store = get_store()
    idn = store.get(name)
    if idn and idn.tier == "owner":
        return {"ok": False, "message": "I won't remove the Owner — that would lock you out."}
    removed = store.remove(name)
    return {"ok": removed,
            "message": f"Removed {name}'s access." if removed else f"I don't have anyone named {name} enrolled."}


def roster() -> list[dict]:
    return [{"name": i.name, "display": i.display, "tier": i.tier, "face": i.face is not None}
            for i in get_store().all()]


def set_passphrase(text: str) -> dict:
    store = get_store()
    if not (text or "").strip():
        store.set_passphrase("")
        return {"ok": True, "message": "Cleared the security passphrase."}
    store.set_passphrase(text)
    return {"ok": True, "message": "Security passphrase set. I'll ask for it before the most "
                                   "sensitive actions."}


# --------------------------------------------------------------------------- #
# Guided enrolment session — the Owner says "add Vikram as trusted", then JARVIS captures the
# new person's face + voice across a few turns. The request lives here; the voice listener runs
# the spoken capture dialog and feeds clips/faces in, then finalises.
# --------------------------------------------------------------------------- #
import threading as _threading

_pending: dict | None = None
_plock = _threading.Lock()
SENTENCES = [
    "The arc reactor is stable and running at full output.",
    "Run a complete diagnostic on the main systems, please.",
    "Good evening — all protocols are online and standing by.",
]
NEEDED_VOICE = 3


def request_enrollment(name: str, tier: str) -> dict:
    """Owner asks to enrol someone. Opens a pending session the listener then drives by voice."""
    tier = (tier or "trusted").strip().lower()
    if tier not in ("trusted", "guest"):
        tier = "trusted"
    if not (name or "").strip():
        return {"ok": False, "message": "Who would you like me to add, sir?"}
    with _plock:
        global _pending
        if _pending:                       # one enrolment at a time — don't clobber an open one
            return {"ok": False, "message": f"I'm in the middle of enrolling {_pending['name']}, "
                                            f"sir — let's finish that first."}
        _pending = {"name": name.strip(), "tier": tier, "clips": [], "faces": [], "sr": 16000}
    return {"ok": True, "name": name.strip(), "tier": tier,
            "message": f"Right, sir. {name.strip()} — look at the camera and read the lines I give "
                       f"you, and I'll learn your face and voice."}


def pending_enrollment() -> dict | None:
    """Lightweight view of the open session (no heavy arrays) for the listener to poll."""
    with _plock:
        if not _pending:
            return None
        return {"name": _pending["name"], "tier": _pending["tier"],
                "have_voice": len(_pending["clips"]), "need_voice": NEEDED_VOICE,
                "have_face": len(_pending["faces"]), "sentences": SENTENCES}


def add_pending_voice(samples, sr: int = 16000) -> dict:
    v = voiceprint.embed(samples, sr)            # heavy embed OUTSIDE the lock
    with _plock:
        if not _pending:
            return {"ok": False, "message": "No enrolment is in progress."}
        if v is None:
            return {"ok": False, "captured": len(_pending["clips"]),
                    "message": "I didn't catch that clearly — let's try the line again."}
        _pending["clips"].append(v)
        return {"ok": True, "captured": len(_pending["clips"]), "need": NEEDED_VOICE}


def add_pending_face(img) -> dict:
    f = face_mod.embed_largest(img)              # heavy embed OUTSIDE the lock
    with _plock:
        if not _pending:
            return {"ok": False, "message": "No enrolment is in progress."}
        if f is None:
            return {"ok": False, "message": "I couldn't see a face — look straight at the camera."}
        _pending["faces"].append(f)
        return {"ok": True, "captured": len(_pending["faces"])}


def cancel_enrollment() -> dict:
    with _plock:
        global _pending
        was = bool(_pending)
        _pending = None
    return {"ok": was, "message": "Enrolment cancelled, sir." if was else "Nothing to cancel."}


def finalize_enrollment() -> dict:
    """Combine the captured voice (and any face) and store the person."""
    with _plock:
        global _pending
        p = _pending
        _pending = None
    if not p:
        return {"ok": False, "message": "No enrolment in progress."}
    if not p["clips"]:
        return {"ok": False, "message": "I didn't get a clear voice sample, sir — let's try again."}
    voice = voiceprint.average(p["clips"])
    facev = face_mod.average(p["faces"]) if p["faces"] else None
    get_store().add(name=p["name"], tier=p["tier"], voice=voice, face=facev,
                    display=p["name"], samples=len(p["clips"]))
    return {"ok": True, "name": p["name"], "tier": p["tier"], "face": facev is not None,
            "message": f"Done, sir — {p['name']} is enrolled as {p['tier']}"
                       + (", face and voice both." if facev is not None else ".")}
