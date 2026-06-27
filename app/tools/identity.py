"""
Identity & access-control tools (Phase 11) — JARVIS's spoken control over his trust roster.

These are the parts that work purely from a voice command:
  * list_access            — who's enrolled and at what tier (Owner only)
  * remove_access          — revoke someone (Owner + passphrase — it's destructive)
  * set_security_passphrase — set/clear the phrase gating the most sensitive actions (Owner)

ENROLLING a new person needs their voice captured (and optionally their face), which is an
interactive recording step — done with `python scripts/enroll_identity.py …` or the app's
enrolment screen, not a single tool call. `enroll_person` below registers the request and tells
the boss exactly how to finish it, rather than pretending to have captured a voice it hasn't.
"""

from __future__ import annotations

import logging

from app.tools import tool
from app.services import identity as ident

logger = logging.getLogger("jarvis.tools.identity")


def _push_identity() -> None:
    """Push the (just-changed) active user to the HUD/mobile name panel, if a loop is running."""
    try:
        import asyncio
        from app.routers.events import broadcast
        asyncio.get_running_loop().create_task(broadcast({"type": "identity", **ident.active_view()}))
    except Exception:  # noqa: BLE001
        pass


@tool(
    "List who has access to you and at what trust tier (Owner, Trusted, Guest). Use when he asks "
    "'who has access', 'who can talk to you', 'who's enrolled', 'who do you recognise'.",
    narration="Checking the access roster",
    tier="owner",
)
def list_access() -> str:
    roster = ident.roster()
    if not roster:
        return ("No one is enrolled yet, sir — so I'm in open mode and answer to anyone. Enrol "
                "your own voice to lock me to you: run  python scripts/enroll_identity.py owner")
    order = {"owner": 0, "trusted": 1, "guest": 2}
    roster.sort(key=lambda r: (order.get(r["tier"], 9), r["display"].lower()))
    lines = ["Access roster:"]
    for r in roster:
        face = " (+face)" if r["face"] else ""
        lines.append(f"- {r['display']} — {r['tier']}{face}")
    return "\n".join(lines)


@tool(
    "Revoke a person's access to you completely (purges their enrolled voice/face). Use when he "
    "says 'remove X's access', 'revoke X', 'forget X', 'X is no longer trusted'. Destructive and "
    "irreversible, so it needs his security passphrase.",
    params={"name": {"type": "string", "description": "the person to remove (his word for them)"}},
    required=["name"],
    narration="Revoking access",
    tier="owner+passphrase",
    terminal=True,
)
def remove_access(name: str) -> str:
    return ident.remove(name)["message"]


@tool(
    "Set or change the security passphrase that gates the most sensitive actions (revoking "
    "access, and any money/vault/irreversible operation). Use when he says 'set my passphrase "
    "to …', 'change the passphrase'. To clear it, pass an empty phrase.",
    params={"passphrase": {"type": "string", "description":
                           "the spoken phrase to require (e.g. 'authorize delta seven'); empty to clear"}},
    required=["passphrase"],
    narration="Updating the security passphrase",
    tier="owner",
    terminal=True,
)
def set_security_passphrase(passphrase: str = "") -> str:
    return ident.set_passphrase(passphrase)["message"]


@tool(
    "Begin enrolling a NEW person so you'll recognise their voice and grant them a tier. Use when "
    "he says 'add my brother Vikram as trusted', 'enrol X as a guest', 'remember this person'. "
    "Enrolling captures their voice (a few spoken lines), so you can't finish it from this call "
    "alone — this registers it and tells him the one quick step to complete it.",
    params={
        "name": {"type": "string", "description": "the new person's name"},
        "tier": {"type": "string", "description": "trusted | guest (never 'owner' here)"},
    },
    required=["name", "tier"],
    narration="Setting up enrolment",
    tier="owner",
    terminal=True,
)
def enroll_person(name: str, tier: str = "trusted") -> str:
    # Open a guided enrolment session; the voice listener captures the person's face + voice over
    # the next few turns and finalises it. (From the app, the enrol screen uploads clips instead.)
    return ident.request_enrollment(name, tier)["message"]


@tool(
    "Re-run FACE recognition through the webcam to confirm who is using JARVIS right now and switch "
    "the active user. Use when he says 'run face recognition again', 're-verify me', 'scan my face', "
    "'who am I', 'check who's here', or when a different person has taken over and you should "
    "re-identify them. Updates the name shown on the HUD.",
    narration="Re-running face recognition",
    tier="guest",
    terminal=True,
)
def reverify_user() -> str:
    import numpy as np
    try:
        from app.services.vision.camera import capture_webcam
        frame = np.array(capture_webcam())[:, :, ::-1].copy()    # PIL RGB -> BGR
    except Exception as e:  # noqa: BLE001
        logger.warning("reverify webcam failed: %s", e)
        return "I couldn't reach the camera, sir."
    t = ident.identify_face(frame)
    if t.tier in ("owner", "trusted", "guest"):
        ident.set_active(t)
        _push_identity()
        if t.is_owner:
            return "Face recognised — welcome back, sir."
        return f"Face recognised — hello, {t.display}. You have {t.tier} access."
    if t.tier == "unsure":
        return "I can't see a face clearly, sir — look straight at the camera and try again."
    return "I don't recognise that face — you're a stranger to me, not on the trusted list."
