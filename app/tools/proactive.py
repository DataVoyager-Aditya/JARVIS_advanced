"""
Proactive tools (Phase 10.F) — let the boss shape JARVIS's self-initiated behaviour by voice.

  * set_routine        — "my gym is at 6", "set my walk to 7:30am", "stop reminding me about the gym"
                         → stores `routines.<name>_time` (+ a `.notify` opt-out) in semantic memory,
                         which the proactive engine reads for its pre-/post-event nudges.
  * proactive_control  — "stop bugging me for an hour", "be quiet", "you can chime in again"
                         → pauses / resumes the engine (persisted, so it survives a restart).

Owner-tier (these shape HIS assistant). Both are terminal — the confirmation is the whole reply.
"""

from __future__ import annotations

import re

from app.tools import tool


def _routine_slug(name: str) -> str:
    base = re.sub(r"[ _]*time$", "", (name or "").strip().lower()).strip()
    return re.sub(r"[^a-z0-9]+", "_", base).strip("_")


@tool(
    "Record or change one of his daily routines so you can nudge him around it (Phase 10.F). Use "
    "when he says things like 'my gym is at 6', 'set my walk to 7:30am', 'I do standup at 10', or to "
    "turn a routine's reminders off/on ('stop reminding me about the gym', 'remind me about my walk "
    "again'). `name` is the routine (gym, walk, standup, lunch...); `when` is the time ('6pm', "
    "'7:30am', '18:00') OR 'off'/'on' to mute/unmute its nudges.",
    params={
        "name": {"type": "string", "description": "the routine — 'gym', 'walk', 'standup', etc."},
        "when": {"type": "string", "description": "a time like '6pm' / '7:30am' / '18:00', or 'off'/'on'"},
    },
    required=["name", "when"],
    narration="Noting the routine",
    terminal=True,
)
def set_routine(name: str, when: str) -> str:
    from app.services.memory import get_memory
    sem = get_memory().semantic
    slug = _routine_slug(name)
    if not slug:
        return "Which routine did you mean, sir?"
    key = f"routines.{slug}_time"
    label = slug.replace("_", " ")
    w = (when or "").strip().lower()
    if w in ("off", "mute", "disable", "stop", "no", "none", "silence"):
        sem.set(f"{key}.notify", "false")
        return f"Done, sir — I'll stop nudging you about your {label}."
    if w in ("on", "enable", "resume", "yes", "unmute"):
        sem.set(f"{key}.notify", "true")
        return f"Nudges for your {label} are back on, sir."
    sem.set(key, when.strip())
    sem.set(f"{key}.notify", "true")
    return f"Noted, sir — your {label} is set for {when.strip()}. I'll give you a heads-up beforehand."


@tool(
    "Pause or resume your habit of speaking up on your own (Phase 10.F). Use when he says 'stop "
    "bugging me', 'be quiet for a bit', 'no interruptions for an hour', or to switch it back on "
    "('you can chime in again', 'resume'). action='pause' (or 'snooze') with optional `minutes` "
    "holds your unprompted remarks; action='resume' turns them back on.",
    params={
        "action": {"type": "string", "description": "pause | snooze | resume"},
        "minutes": {"type": "integer", "description": "for pause/snooze: how long to stay quiet (default 60)"},
    },
    required=["action"],
    narration="Adjusting how often I chime in",
    terminal=True,
)
def proactive_control(action: str, minutes: int = 60) -> str:
    from app.services import proactive
    a = (action or "").lower().strip()
    if a in ("resume", "on", "enable", "unmute", "unpause"):
        proactive.resume()
        return "Understood, sir — I'll chime in when it's genuinely worth it."
    try:
        mins = max(1, int(float(minutes)))
    except (TypeError, ValueError):
        mins = 60
    proactive.pause(mins)
    if mins >= 1440:
        return "I'll keep to myself, sir — no unprompted remarks until you say otherwise."
    if mins >= 120:
        hrs = round(mins / 60, 1)
        return f"I'll hold my tongue for about {hrs:g} hours, sir."
    return f"I'll stay quiet for {mins} minutes, sir."
