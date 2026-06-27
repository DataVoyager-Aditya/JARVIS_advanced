"""
Phase 11 — Identity, recognition & access control.

Only the Owner gets the full JARVIS; people he enrolls get limited access; unknown voices get
nothing. Two free, local biometric factors:
  * voice  — resemblyzer 256-d voiceprint (primary; works from across the room)
  * face   — OpenCV YuNet+SFace 128-d (optional second factor when a camera frame is present)

Public API (used by the agent, the listener and the tools):

    resolve(channel, text, voice, sr, face_img) -> Trust   # who is this & what may they do
    tool_allowed(tool_tier, trust) -> bool                 # enforce a tool's min tier
    prompt_line(trust) -> str                              # in-character "who am I serving"
    enroll(name, tier, clips, …) / enroll_owner(…)         # enrollment
    remove(name) / roster() / set_passphrase(text)
    is_enrolled() / enabled()
"""

from __future__ import annotations

from config import IDENTITY_ENABLED
from .trust import (Trust, resolve, identify_voice, identify_face, confirm_face, tool_allowed,
                    tool_visible, prompt_line, is_enrolled, reset,
                    set_active, get_active, active_view,
                    set_session_trust, get_session_trust)
from .enrollment import (enroll, enroll_owner, remove, roster, set_passphrase,
                         request_enrollment, pending_enrollment, add_pending_voice,
                         add_pending_face, finalize_enrollment, cancel_enrollment)
from .store import get_store


def enabled() -> bool:
    return IDENTITY_ENABLED


__all__ = ["Trust", "resolve", "identify_voice", "identify_face", "confirm_face", "tool_allowed",
           "tool_visible", "prompt_line", "is_enrolled", "reset", "set_active", "get_active",
           "active_view", "set_session_trust", "get_session_trust",
           "enroll", "enroll_owner", "remove", "roster", "set_passphrase",
           "request_enrollment", "pending_enrollment", "add_pending_voice", "add_pending_face",
           "finalize_enrollment", "cancel_enrollment", "get_store", "enabled"]
