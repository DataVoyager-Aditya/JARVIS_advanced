"""
Call tools (Phase 8) — JARVIS's control over the phone line, via the Android companion.

`phone_call_action` is the one verb the agent calls for everything call-related: act on the
call ringing right now (decline / silence / answer), or report on missed/recent calls. It
NEVER fabricates — if the companion isn't set up or no call is live, it says so plainly.

`set_call_rule` (Phase 8.5) sets a per-contact auto-handle rule the companion enforces on the
phone itself (auto-text "I'll call you back", auto-answer on speaker, or auto-decline).

All state lives in the calls service (queued commands + persistent call log); these tools are
thin, in-character wrappers over it.
"""

from __future__ import annotations

import re
import time

from config import CH_CALL
from app.tools import tool
from app.services import calls
from app.services.calls.store import get_call_store, MISSED, OUTGOING


def _ago(ts: float) -> str:
    d = max(0, time.time() - ts)
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return time.strftime("%a %H:%M", time.localtime(ts))


@tool(
    "Act on phone calls via his Android companion. Use action='decline' (hang up), 'silence' "
    "(mute the ringer), or 'answer' (pick up on speaker) for the call ringing RIGHT NOW — only "
    "when he tells you to. To decline AND text the caller back (e.g. 'decline and tell them I'm "
    "busy, I'll call later'), use action='decline' WITH the `message` arg — it hangs up and sends "
    "that as an SMS. Use action='read_missed' for missed calls, or 'recent' for the call log. To "
    "place an outbound call use place_call, not this. If the companion isn't set up or nothing's "
    "ringing, it tells you — relay that, don't pretend.",
    params={
        "action": {"type": "string", "description":
                   "decline | silence | answer | read_missed | recent"},
        "message": {"type": "string", "description":
                    "optional — with action='decline', an SMS to send the caller (e.g. 'busy, "
                    "call you back'). Compose it naturally from what he says."},
    },
    required=["action"],
    narration="Handling the call",
    terminal=False,    # reads + confirmations go through the model so JARVIS phrases them
                       # naturally ("two missed calls from Mom on Sunday") instead of reading the
                       # raw list aloud. The command itself is queued the moment this runs.
)
def phone_call_action(action: str, message: str = "") -> str:
    act = (action or "").lower().strip()

    if act in ("read_missed", "missed", "missed_calls", "any_missed"):
        rows = get_call_store().missed(limit=8)
        get_call_store().mark_seen(kind=MISSED)
        if not rows:
            return "No missed calls — he's all caught up."
        lines = [f"{len(rows)} missed call{'s' if len(rows) > 1 else ''} (relay this naturally, "
                 "by his name for the caller, not as a list):"]
        for c in rows:
            lines.append(f"- {calls._nice_name(c.name, c.number)}, {_ago(c.ts)}")
        return "\n".join(lines)

    if act in ("recent", "log", "call_log", "history"):
        rows = get_call_store().recent(limit=8)
        if not rows:
            return "No calls in the log yet."
        label = {"incoming": "incoming", "missed": "missed", "answered": "answered",
                 "declined": "declined", "ended": "ended", "outgoing": "outgoing"}
        lines = ["Recent calls (relay naturally, by his name for each caller):"]
        for c in rows:
            lines.append(f"- {calls._nice_name(c.name, c.number)} — {label.get(c.kind, c.kind)}, {_ago(c.ts)}")
        return "\n".join(lines)

    if act in ("callback", "call_back", "call", "dial", "ring"):
        # Outbound dialing lives in place_call; nudge the model there instead of claiming we can't.
        return "To place a call, use the place_call tool with the contact's name."

    # Otherwise it's a command for the live ring (decline / silence / answer / ignore),
    # optionally declining WITH a text to the caller.
    res = calls.queue_command(act, message=message)
    return res["message"]


@tool(
    "Set how a caller is auto-handled on his phone when they call (Phase 8.5). action='auto_text' "
    "declines and texts them a line (use the `message` arg, e.g. 'in a meeting, call you back'); "
    "'auto_answer' picks up on speaker hands-free; 'auto_decline' silently rejects; 'off' removes "
    "the rule. The companion enforces it on the phone. Only set this when he asks.",
    params={
        "contact": {"type": "string", "description":
                    "the caller's name or number to match (his word for them is fine)"},
        "action": {"type": "string", "description":
                   "auto_text | auto_answer | auto_decline | off"},
        "message": {"type": "string", "description":
                    "for auto_text: the line to text them (optional; a sensible default is used)"},
    },
    required=["contact", "action"],
    narration="Setting the call rule",
    terminal=True,
)
def set_call_rule(contact: str, action: str, message: str = "") -> str:
    return calls.set_rule(contact, action, message)["message"]


@tool(
    "Place a phone call FROM his phone to a person or number (Phase 8.5 — dialing). His companion "
    "dials it; he then talks on his phone normally (you can't speak on the call yourself). Use when "
    "he says 'call X', 'dial X', 'ring X', 'phone X'. Pass his word for the person; their number "
    "comes from his contacts file. If there's no number for them, say so — don't invent one.",
    params={
        "contact": {"type": "string", "description":
                    "who to call — his word for them ('Mom', 'Farhan') or a phone number"},
    },
    required=["contact"],
    narration="Placing the call",
    terminal=True,
)
def place_call(contact: str) -> str:
    from app.services.messaging import contacts
    raw = (contact or "").strip()
    if not raw:
        return "Who would you like me to call?"
    # fuzzy=True so a partial first name works ("Om" -> "Om Tiwari Pandey").
    number = contacts.resolve(raw, CH_CALL, fuzzy=True)   # name -> number; a number passes through
    digits = re.sub(r"[^\d+]", "", number)
    if len(re.sub(r"\D", "", digits)) < 7:           # not enough digits to be a real number
        return f"I don't have a number saved for {raw}, sir. Give me the number and I'll dial it."
    res = calls.queue_dial(digits)
    if not res.get("ok"):
        return res.get("message", "Couldn't place the call.")
    get_call_store().add(OUTGOING, number=digits, name=raw if raw != digits else "",
                         ref=f"dial-{digits}-{int(time.time())}")
    who = contacts.display(digits, CH_CALL)
    label = who if who and who != digits else raw
    return f"Calling {label} on your phone now."


@tool(
    "Clear his phone-call log / call history. Use when he says 'clear my call log', 'clear my "
    "missed calls', 'forget those calls', 'wipe my call history', 'clear old calls'. scope='all' "
    "wipes the whole log, 'missed' clears just missed calls, 'old' clears anything older than a day.",
    params={"scope": {"type": "string", "description": "all | missed | old"}},
    narration="Clearing the call log",
    terminal=True,
)
def clear_call_log(scope: str = "all") -> str:
    res = calls.clear_log(scope)
    n = res["n"]
    if n == 0:
        return "Your call log's already clear, sir."
    noun = {"missed": "missed call", "old": "old call", "all": "call"}[res["scope"]]
    return f"Cleared {n} {noun}{'s' if n != 1 else ''} from your log, sir."


@tool(
    "List the per-contact call auto-handle rules currently set (Phase 8.5). Use when he asks "
    "what call rules are active.",
    narration="Checking call rules",
)
def list_call_rules() -> str:
    rules = calls.get_rules()
    if not rules:
        return "No call auto-handle rules are set."
    pretty = {"auto_text": "auto-text", "auto_answer": "auto-answer (speaker)",
              "auto_decline": "auto-decline"}
    lines = ["Call rules:"]
    for r in rules:
        extra = f" — \"{r['message']}\"" if r["action"] == "auto_text" and r["message"] else ""
        lines.append(f"- {r['match']}: {pretty.get(r['action'], r['action'])}{extra}")
    return "\n".join(lines)
