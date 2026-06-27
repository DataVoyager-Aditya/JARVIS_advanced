"""
Proactive endpoints (Phase 10.F) — JARVIS speaking up on his own.

The desktop voice listener drives this:
  POST /proactive/poll    the listener reports its state (in a conversation? idle how long?) and
                          gets back an optional line to speak. The SERVER decides whether/what — so
                          all the gating (quiet hours, mood, caps, owner-only, coin) lives in one
                          place and the listener stays dumb. Returns {say, kind, key, expects_reply}.
  POST /proactive/ack     the listener confirms it actually SPOKE the line {kind, key}; only now does
                          it count toward the daily cap / min-gap / dedup (so a dropped line is free).
  GET  /proactive/status  current state (enabled/paused/today's count/register) for debug + the HUD.

`poll` is open (local listener, read-mostly — it only ever yields a line for JARVIS to say, and the
engine itself refuses to speak to anyone but the Owner), mirroring /messaging/announcements.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from app.services import proactive

logger = logging.getLogger("jarvis.proactive.router")
router = APIRouter(prefix="/proactive", tags=["proactive"])


class PollReq(BaseModel):
    in_conversation: bool = False
    idle_s: float = 0.0
    channel: str = "pc_voice"


class AckReq(BaseModel):
    kind: str
    key: str = ""


@router.post("/poll")
async def poll(req: PollReq) -> dict:
    try:
        return await proactive.poll(req.model_dump())
    except Exception:  # noqa: BLE001
        logger.exception("proactive poll failed")
        return {"say": None, "kind": None, "key": None, "expects_reply": False}


@router.post("/ack")
async def ack(req: AckReq) -> dict:
    try:
        proactive.ack(req.kind, req.key)
        return {"ok": True}
    except Exception:  # noqa: BLE001
        logger.exception("proactive ack failed")
        return {"ok": False}


@router.get("/status")
async def status() -> dict:
    return proactive.status()
