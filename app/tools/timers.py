"""Timer / reminder tools — persistent, real Windows toasts, survive restart."""

from __future__ import annotations

import datetime as _dt

from app.tools import tool
from app.services.agent.scheduler import get_scheduler, parse_when


@tool(
    "Set a countdown timer / alarm. Use for 'set a timer for N minutes', 'wake me in N', "
    "'alarm in N minutes'. Always tell the user the exact clock time it will go off.",
    params={"minutes": {"type": "number", "description": "minutes from now"},
            "seconds": {"type": "number", "description": "extra seconds (optional)"},
            "label": {"type": "string", "description": "what the timer is for (optional)"}},
    required=["minutes"],
    narration="Setting that timer",
)
def set_timer(minutes: float = 0, seconds: float = 0, label: str = "") -> str:
    total = float(minutes) * 60 + float(seconds)
    if total <= 0:
        return "That timer length doesn't make sense — give me a positive duration."
    fire = _dt.datetime.now() + _dt.timedelta(seconds=total)
    text = label.strip() or "Timer's up."
    get_scheduler().add(fire, text, kind="timer")
    clock = fire.strftime("%I:%M %p").lstrip("0")
    mins = int(total // 60)
    secs = int(total % 60)
    dur = (f"{mins} min" if mins else "") + (f" {secs} sec" if secs else "")
    return f"Timer set for {dur.strip()} — it'll go off at {clock}."


@tool(
    "Set a reminder for a specific time. Use for 'remind me at 8pm to X', 'remind me "
    "tomorrow at 9 to X'. State the resolved clock time back.",
    params={"when": {"type": "string", "description": "when, e.g. 'at 8pm', 'tomorrow 9am', 'in 2 hours', '20:30'"},
            "text": {"type": "string", "description": "what to remind about"}},
    required=["when", "text"],
    narration="Setting that reminder",
)
def set_reminder(when: str, text: str) -> str:
    parsed = parse_when(when)
    if parsed is None:
        return f"I couldn't work out when '{when}' is — try 'at 8pm' or 'in 2 hours'."
    fire, clock = parsed
    get_scheduler().add(fire, text.strip() or "Reminder.", kind="reminder")
    day = "" if fire.date() == _dt.date.today() else fire.strftime(" on %A") if (fire.date() - _dt.date.today()).days < 7 else fire.strftime(" on %d %b")
    return f"Reminder set for {clock}{day} — {text.strip()}."


@tool("List all pending timers and reminders with their fire times.", narration="")
def list_reminders() -> str:
    pending = get_scheduler().list_pending()
    if not pending:
        return "Nothing scheduled."
    lines = []
    for r in pending:
        clock = _dt.datetime.fromtimestamp(r.fire_at).strftime("%a %I:%M %p").replace(" 0", " ")
        lines.append(f"#{r.id} [{r.kind}] {clock} — {r.text}")
    return "\n".join(lines)


@tool(
    "Cancel pending timers/reminders by description. Use for 'cancel my gym reminder', "
    "'cancel all timers', 'cancel the next one'. `which` can be 'all', 'next', or words that "
    "match the reminder text or kind (timer/reminder).",
    params={"which": {"type": "string", "description": "'all', 'next', or words matching the item to cancel"}},
    required=["which"],
    narration="",
)
def cancel_reminders(which: str = "all") -> str:
    s = get_scheduler()
    pending = s.list_pending()
    if not pending:
        return "Nothing scheduled to cancel."
    w = which.lower().strip()
    if w in ("all", "everything", "them all", "every", "any"):
        n = s.cancel_all()
        return f"Cancelled {n} item{'s' if n != 1 else ''}."
    if w in ("next", "first", "the next one", "next one", "soonest"):
        s.cancel(pending[0].id)
        return f"Cancelled the next one — {pending[0].text}."
    # match by kind or by any meaningful word overlapping the reminder text
    _STOP = {"the", "a", "my", "reminder", "reminders", "timer", "timers", "alarm", "alarms",
             "to", "at", "one", "for", "about", "that", "please", "cancel", "stop", "and"}
    tokens = [t for t in w.split() if t not in _STOP]
    targets = [r for r in pending
               if (w in r.text.lower() or w == r.kind
                   or any(tok in r.text.lower() or tok == r.kind for tok in tokens))]
    if not targets:
        return f"Couldn't find a pending item matching '{which}'. Say 'list reminders' to hear them."
    for r in targets:
        s.cancel(r.id)
    if len(targets) == 1:
        return f"Cancelled — {targets[0].text}."
    return f"Cancelled {len(targets)} matching items."


@tool(
    "Turn off / stop an alarm: silences one that's ringing now AND clears any pending "
    "(not-yet-fired) timers so it won't go off. Use for 'turn off the alarm', 'stop the "
    "alarm', 'kill the timer'. For reminders by name, use cancel_reminders instead.",
    narration="",
)
def stop_alarm() -> str:
    s = get_scheduler()
    s.stop_ringing()
    timers = [r for r in s.list_pending() if r.kind == "timer"]
    n = sum(1 for r in timers if s.cancel(r.id))
    if n:
        return f"Stopped — cleared {n} timer{'s' if n != 1 else ''}."
    return "Stopped."


@tool(
    "Snooze the alarm/reminder that just went off, for a number of minutes (default 10).",
    params={"minutes": {"type": "number", "description": "snooze length in minutes (default 10)"}},
    narration="Snoozing that",
)
def snooze_alarm(minutes: float = 10) -> str:
    s = get_scheduler()
    s.stop_ringing()
    last = s.last_fired
    if last is None:
        return "Nothing recent to snooze."
    fire = _dt.datetime.now() + _dt.timedelta(minutes=float(minutes))
    s.add(fire, last.text, kind=last.kind)
    clock = fire.strftime("%I:%M %p").lstrip("0")
    return f"Snoozed — I'll nudge you again at {clock}."
