"""
Call endpoints (Phase 8) — the bridge between the Android companion and JARVIS.

Companion -> JARVIS (token-gated, only your phone may post):
  POST /calls/incoming     a call is ringing      {number,name,ts,ref}
  POST /calls/missed       a call was missed       {number,name,ts,ref}
  POST /calls/ended        the ring ended          {number,ref,answered}
  POST /calls/sync         bulk CallLog upsert      {entries:[{kind,number,name,ts,ref}]}
  GET  /calls/commands     long-poll for queued commands (decline/silence/answer)

JARVIS/PWA -> companion:
  POST /calls/command      queue a command          {action}
  GET  /calls/recent       recent call log (HUD/debug)
  GET  /calls/announcements desktop listener drains spoken ring/missed lines

The companion-facing routes require the shared CALLS_WEBHOOK_TOKEN header so nothing on the
network but your own phone can drive the phone line.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from config import CALLS_WEBHOOK_TOKEN
from app.services import calls

logger = logging.getLogger("jarvis.calls.router")
router = APIRouter(prefix="/calls", tags=["calls"])


def _auth(token: str) -> None:
    if token != CALLS_WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")


class CallIn(BaseModel):
    number: str = ""
    name: str = ""
    ts: float | None = None
    ref: str = ""


class EndedIn(BaseModel):
    number: str = ""
    ref: str = ""
    answered: bool = False


class SyncIn(BaseModel):
    entries: list[dict] = []


class CommandIn(BaseModel):
    action: str


@router.post("/incoming")
async def incoming(call: CallIn, x_jarvis_token: str = Header(default="")):
    _auth(x_jarvis_token)
    res = await calls.record_incoming(number=call.number, name=call.name, ts=call.ts, ref=call.ref)
    return {"ok": True, **res}


@router.post("/missed")
async def missed(call: CallIn, x_jarvis_token: str = Header(default="")):
    _auth(x_jarvis_token)
    res = await calls.record_missed(number=call.number, name=call.name, ts=call.ts, ref=call.ref)
    return {"ok": True, **res}


@router.post("/ended")
async def ended(ev: EndedIn, x_jarvis_token: str = Header(default="")):
    _auth(x_jarvis_token)
    res = await calls.record_ended(number=ev.number, ref=ev.ref, answered=ev.answered)
    return {"ok": True, **res}


@router.post("/sync")
async def sync(body: SyncIn, x_jarvis_token: str = Header(default="")):
    _auth(x_jarvis_token)
    new = calls.sync_log(body.entries)
    return {"ok": True, "new": new}


@router.get("/commands")
async def commands(x_jarvis_token: str = Header(default="")):
    """Companion long-poll. Returns any queued commands and clears them."""
    _auth(x_jarvis_token)
    return {"commands": calls.take_commands()}


@router.post("/command")
async def command(req: CommandIn):
    """JARVIS (via the tool) or the PWA (a button tap) queues a call command."""
    return calls.queue_command(req.action)


@router.get("/recent")
async def recent(limit: int = 10):
    from app.services.calls.store import get_call_store
    rows = get_call_store().recent(limit=limit)
    live = calls.pending()
    return {
        "calls": [{"kind": c.kind, "name": c.display, "number": c.number,
                   "ts": c.ts, "when": c.when} for c in rows],
        "ringing": live,
    }


@router.get("/announcements")
async def announcements():
    """The desktop voice listener long-polls this; we hand back any pending spoken items
    ({line, kind}). `lines` is kept for any plain-text consumer."""
    items = calls.drain_spoken()
    return {"items": items, "lines": [i["line"] for i in items]}


@router.get("/dial", response_class=PlainTextResponse)
async def dial(x_jarvis_token: str = Header(default="")):
    """Plain-text dial poll for a simple Macrodroid macro: returns just the number to dial
    (once), or the literal `none` when there's nothing to dial. We return `none` (not an empty
    body) on purpose — some Macrodroid builds DON'T overwrite a variable with an empty HTTP
    response, which would leave the last number stuck and re-dial every poll. A non-empty `none`
    forces the overwrite, and `none` has no digit so the caller's `[0-9]` constraint won't fire."""
    _auth(x_jarvis_token)
    return calls.take_dial() or "none"


@router.get("/rules")
async def rules(x_jarvis_token: str = Header(default="")):
    """Companion pulls the auto-handle rules (Phase 8.5) and caches them to act on a ring."""
    _auth(x_jarvis_token)
    return {"rules": calls.get_rules()}
