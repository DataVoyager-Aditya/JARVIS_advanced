"""
Phase 8 — Calls service (Android companion bridge).

The phone companion (Macrodroid recipe OR the bundled Kotlin app) is JARVIS's link to the
actual phone line. It POSTs call events here and long-polls for commands:

    record_incoming / record_missed / record_ended / sync_log   <- companion POSTs
    take_commands()                                              <- companion long-polls
    queue_command(action)                                        <- JARVIS queues (tool / PWA tap)
    drain_spoken()                                               <- desktop listener speaks
    pending()                                                    <- the live ring, if any

Two ephemeral, in-process surfaces (a ring only matters while it's ringing, and you don't want
a backlog of old rings spoken on restart): the spoken-announcement buffer and the command
queue. The call LOG itself is always persisted in the store, so nothing durable is lost.

Name resolution reuses the Phase-7 contacts map ([call] section, then [common]) so a saved
"Mummy" is announced as "Mom" the way he refers to her.
"""

from __future__ import annotations

import logging
import threading
import time

from config import CH_CALL, CALL_RING_TTL_S
from . import store as _store_mod
from .store import get_call_store, INCOMING, MISSED, ANSWERED, DECLINED, ENDED, OUTGOING

logger = logging.getLogger("jarvis.calls")

# Commands the companion understands, with the spoken/typed synonyms JARVIS may map to them.
_ACTIONS = {
    "decline": "decline", "reject": "decline", "hang up": "decline", "hangup": "decline",
    "dismiss": "decline", "end": "decline", "busy": "decline", "cut": "decline",
    "silence": "silence", "mute": "silence", "quiet": "silence", "shut up": "silence",
    "answer": "answer", "accept": "answer", "pick up": "answer", "pickup": "answer",
    "take it": "answer", "take the call": "answer",
    "ignore": "ignore", "let it ring": "ignore", "leave it": "ignore", "nothing": "ignore",
}

# --- ephemeral state (per process) ----------------------------------------- #
_lock = threading.Lock()
_pending: dict | None = None       # the call currently ringing {call_id, name, number, ts, ref}
_commands: list[dict] = []         # queued for the companion to pick up
_spoken: list[dict] = []           # spoken lines the desktop listener drains
_last_dial: str = ""               # plain pending dial number (simple companions poll /calls/dial)
_SPOKEN_MAX = 20


def _nice_name(name: str, number: str) -> str:
    """What JARVIS should CALL the caller. Priority:
      1. a number -> relationship mapping in MY_CONTACTS [call] ("Mom = 9871521319") — so
         even though the phone sends "Sandhya", he says "Mom". Number match is substring-tolerant,
         so storing the 10-digit number still matches a +91… caller id.
      2. else map the phone-supplied name (Sandhya -> Mom if mapped), or use it as-is.
      3. else the bare number, else "an unknown number"."""
    from app.services.messaging import contacts
    num = (number or "").strip()
    if num:
        by_num = contacts.display(num, CH_CALL)
        if by_num and by_num != num:          # a [call] number rule matched -> his word for them
            return by_num
    base = (name or "").strip()
    if base:
        return contacts.display(base, CH_CALL)
    return num or "an unknown number"


def _buffer_spoken(line: str, kind: str) -> None:
    with _lock:
        _spoken.append({"line": line, "kind": kind, "ts": time.time()})
        if len(_spoken) > _SPOKEN_MAX:
            del _spoken[:-_SPOKEN_MAX]


async def _broadcast(event: dict) -> None:
    try:
        from app.routers.events import broadcast
        await broadcast(event)
    except Exception as e:  # noqa: BLE001
        logger.debug("call HUD broadcast failed: %s", e)


# --- companion -> JARVIS (inbound events) ---------------------------------- #
async def record_incoming(number: str = "", name: str = "", ts: float | None = None,
                          ref: str = "") -> dict:
    """A call is ringing now. Persist it, mark it the live ring, announce on PC + PWA."""
    global _pending
    ts = time.time() if ts is None else ts
    cid = get_call_store().add(INCOMING, number=number, name=name, ts=ts, ref=ref, seen=1)
    disp = _nice_name(name, number)
    with _lock:
        _pending = {"call_id": cid, "name": disp, "number": number, "ts": ts, "ref": ref}
    _buffer_spoken(f"Sir, {disp} is calling. Say “JARVIS, decline” or “answer”, "
                   f"or use the app.", INCOMING)
    await _broadcast({"type": "call", "kind": INCOMING, "name": disp, "number": number,
                      "call_id": cid, "ts": ts, "actions": ["answer", "decline", "silence"]})
    logger.info("incoming call from %s (%s)", disp, number or "no number")
    return {"call_id": cid, "name": disp}


async def record_missed(number: str = "", name: str = "", ts: float | None = None,
                        ref: str = "") -> dict:
    """A call was missed. Persist it and announce it softly (no prompt — nothing to act on)."""
    global _pending
    ts = time.time() if ts is None else ts
    cid = get_call_store().add(MISSED, number=number, name=name, ts=ts, ref=ref, seen=1)
    if cid is None:  # already knew about this missed call (re-sync) — don't re-announce
        return {"call_id": None, "duplicate": True}
    disp = _nice_name(name, number)
    with _lock:  # a missed call clears any matching live ring
        if _pending and (_pending.get("ref") == ref or _pending.get("number") == number):
            _pending = None
    _buffer_spoken(f"Sir, you have a missed call from {disp}.", MISSED)
    await _broadcast({"type": "call", "kind": MISSED, "name": disp, "number": number,
                      "call_id": cid, "ts": ts})
    logger.info("missed call from %s (%s)", disp, number or "no number")
    return {"call_id": cid, "name": disp}


async def record_ended(number: str = "", ref: str = "", answered: bool = False) -> dict:
    """The ring ended (answered on the phone, declined, or rang out). Clear the live ring."""
    global _pending
    kind = ANSWERED if answered else ENDED
    get_call_store().add(kind, number=number, ref=ref or "", ts=time.time())
    with _lock:
        was = _pending
        if _pending and (not ref or _pending.get("ref") == ref
                         or _pending.get("number") == number or not number):
            _pending = None
    await _broadcast({"type": "call", "kind": "cleared", "number": number})
    return {"cleared": bool(was)}


def sync_log(entries: list[dict]) -> int:
    """Bulk-upsert the phone's recent CallLog so missed-call queries work even without a live
    push. Each entry: {kind, number, name, ts, ref}. Returns how many were NEW."""
    st = get_call_store()
    new = 0
    for e in entries or []:
        kind = (e.get("kind") or "").lower().strip()
        if kind not in (INCOMING, MISSED, ANSWERED, DECLINED, ENDED, OUTGOING):
            kind = MISSED if "miss" in kind else ENDED
        cid = st.add(kind, number=str(e.get("number", "")), name=str(e.get("name", "")),
                     ts=float(e.get("ts") or time.time()), ref=str(e.get("ref", "")), seen=1)
        if cid is not None:
            new += 1
    return new


# --- JARVIS -> companion (outbound commands) ------------------------------- #
def normalize_action(action: str) -> str:
    """Map a spoken/typed phrase to a canonical command (decline/silence/answer/ignore)."""
    a = (action or "").lower().strip()
    if a in _ACTIONS:
        return _ACTIONS[a]
    for phrase, canon in _ACTIONS.items():
        if phrase in a:
            return canon
    return ""


def queue_command(action: str, message: str = "") -> dict:
    """Queue a call command for the companion. Only meaningful while a call is LIVE (ringing).
    A `message` together with a decline = hang up AND text the caller ("busy, call you back").
    Returns {ok, live, action, name, message}."""
    global _pending
    canon = normalize_action(action)
    if not canon:
        return {"ok": False, "live": False, "action": "",
                "message": f"I don't know how to '{action}' a call."}
    with _lock:
        p = _pending
        live = bool(p) and (time.time() - p["ts"] <= CALL_RING_TTL_S)
        name = p["name"] if p else ""
        if not live:
            return {"ok": False, "live": False, "action": canon, "name": name,
                    "message": "There's no call ringing right now."}
        if canon == "ignore":
            return {"ok": True, "live": True, "action": "ignore", "name": name,
                    "message": f"Letting {name}'s call ring."}
        cmd = {"action": canon, "call_id": p["call_id"], "number": p["number"], "ts": time.time()}
        if canon == "decline" and message.strip():
            cmd["message"] = message.strip()      # hang up AND text the caller this
        _commands.append(cmd)
        if canon in ("decline", "answer"):
            _pending = None  # acted on — no longer the live ring
    spoken = {"decline": f"Declining {name}'s call.",
              "silence": f"Silencing {name}'s call.",
              "answer": f"Answering {name}'s call."}[canon]
    if canon == "decline" and message.strip():
        spoken = f"Declining {name}'s call and texting them."
    return {"ok": True, "live": True, "action": canon, "name": name, "message": spoken}


def take_commands() -> list[dict]:
    """Companion long-poll: return and clear queued commands."""
    with _lock:
        cmds = list(_commands)
        _commands.clear()
    return cmds


def queue_dial(number: str) -> dict:
    """Queue an OUTBOUND call for the companion to place on the phone (Phase 8.5 — dialing).
    Unlike ring commands this needs no live call. Goes into the command queue (Kotlin app) AND a
    plain `_last_dial` slot (so a simple Macrodroid macro can GET just the number). Returns
    {ok, number, message}."""
    global _last_dial
    num = (number or "").strip()
    if not num:
        return {"ok": False, "message": "No number to dial."}
    with _lock:
        _commands.append({"action": "dial", "number": num, "ts": time.time()})
        _last_dial = num
    return {"ok": True, "number": num}


def take_dial() -> str:
    """Plain-number dial poll for simple companions (Macrodroid): returns the queued dial number
    once, then clears it. Empty string when there's nothing to dial."""
    global _last_dial
    with _lock:
        num = _last_dial
        _last_dial = ""
    return num


# --- desktop listener + tools ---------------------------------------------- #
def pending() -> dict | None:
    with _lock:
        if _pending and (time.time() - _pending["ts"] <= CALL_RING_TTL_S):
            return dict(_pending)
        return None


def drain_spoken() -> list[dict]:
    """The desktop voice listener drains this and speaks each line. Each item carries its `kind`
    so the listener knows an `incoming` line expects a spoken reply (decline/answer) and can open
    a brief hands-free command window — a `missed` line just gets spoken."""
    with _lock:
        items = [{"line": it["line"], "kind": it["kind"]} for it in _spoken]
        _spoken.clear()
    return items


# --- auto-handle rules (Phase 8.5) ----------------------------------------- #
_RULE_ACTIONS = {"auto_text", "auto_answer", "auto_decline"}


def set_rule(match: str, action: str, message: str = "") -> dict:
    """Set a per-contact auto-handle rule the companion enforces. Returns {ok, message}."""
    action = (action or "").lower().strip()
    if action in ("off", "none", "clear", "stop"):
        removed = get_call_store().clear_rule(match)
        return {"ok": True, "message": f"Auto-handling for {match} turned off." if removed
                else f"There was no auto-handle rule for {match}."}
    if action not in _RULE_ACTIONS:
        return {"ok": False, "message": f"Unknown auto-handle action '{action}'."}
    if action == "auto_text" and not message:
        message = "Can't take your call right now — I'll call you back shortly. (sent by JARVIS)"
    get_call_store().set_rule(match, action, message)
    return {"ok": True, "message": f"Set {action.replace('_', '-')} for {match}.", "action": action}


def get_rules() -> list[dict]:
    """The companion pulls these and caches them, so it can act on a ring without a round-trip."""
    return get_call_store().rules()


def clear_log(scope: str = "all") -> dict:
    """Clear the persistent call log. scope: 'all' wipes everything; 'missed' clears missed calls;
    'old' clears anything older than 24h (keeps today's). Returns {n, scope}."""
    st = get_call_store()
    scope = (scope or "all").lower().strip()
    if scope in ("missed", "missed_calls", "missed calls"):
        return {"n": st.clear(kind=MISSED), "scope": "missed"}
    if scope in ("old", "older", "history", "stale"):
        return {"n": st.clear(before=time.time() - 86400), "scope": "old"}
    return {"n": st.clear(), "scope": "all"}


__all__ = ["record_incoming", "record_missed", "record_ended", "sync_log",
           "queue_command", "queue_dial", "take_dial", "take_commands", "normalize_action",
           "pending", "drain_spoken", "set_rule", "get_rules"]
