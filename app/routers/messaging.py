"""
Messaging endpoints (Phase 7).

  POST /messaging/whatsapp/incoming   the Node sidecar pushes new WhatsApp messages here
  GET  /messaging/inbox               unified, ranked inbox (backs the PWA comms panel)
  GET  /messaging/status              which channels are connected + unread counts
  POST /messaging/send                generic send {channel,to,body} (PWA one-tap reply)
  GET  /messaging/announcements       voice listener drains spoken new-message lines

The webhook is gated by a shared token so only our local sidecar can post to it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from config import (WHATSAPP_WEBHOOK_TOKEN, CH_WHATSAPP, CH_INSTAGRAM, CH_EMAIL)
from app.services.messaging import get_messaging, unified, notify

logger = logging.getLogger("jarvis.messaging.router")
router = APIRouter(prefix="/messaging", tags=["messaging"])


class WhatsAppIn(BaseModel):
    name: str = ""
    number: str = ""
    body: str = ""
    chat_id: str = ""
    group: str = ""          # group name (only for @g.us group messages)
    is_group: bool = False
    ref: str = ""


@router.post("/whatsapp/incoming")
async def whatsapp_incoming(msg: WhatsAppIn, x_jarvis_token: str = Header(default="")):
    if x_jarvis_token != WHATSAPP_WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")
    stored = await get_messaging().handle_whatsapp_in(
        name=msg.name, number=msg.number, body=msg.body, chat_id=msg.chat_id,
        group=msg.group, is_group=msg.is_group, ref=msg.ref)
    return {"ok": True, "stored": stored}


@router.get("/inbox")
async def inbox(limit: int = 12, only_unread: bool = False):
    """Merged, ranked messages for the HUD comms panel."""
    items = unified.unified(limit=limit, only_unread=only_unread)
    return {"items": items, "unread": get_messaging().store.unread_counts()}


@router.get("/status")
async def status():
    return await get_messaging().status()


class SendReq(BaseModel):
    channel: str
    to: str
    body: str


@router.post("/send")
async def send(req: SendReq):
    """One surface for the PWA to reply on any channel."""
    import asyncio
    ch = (req.channel or "").lower().strip()
    to, body = req.to.strip(), req.body
    if not to or not body:
        raise HTTPException(status_code=400, detail="need to + body")
    try:
        if ch == CH_EMAIL:
            from app.services.messaging.email_client import get_email
            c = get_email()
            if not c.enabled:
                return {"ok": False, "error": "email not connected"}
            original = await asyncio.to_thread(c.find, to)
            if original:
                await asyncio.to_thread(c.reply, original, body)
                target = original.from_addr
            else:
                await asyncio.to_thread(c.send, to, "", body)
                target = to
        elif ch == CH_WHATSAPP:
            from app.services.messaging.whatsapp_client import get_whatsapp
            res = await get_whatsapp().send(to, body)
            if res.get("error"):
                return {"ok": False, "error": res["error"]}
            target = res.get("to", to)
        elif ch == CH_INSTAGRAM:
            from app.services.messaging.instagram import get_instagram
            target = await asyncio.to_thread(get_instagram().send_dm, to, body)
        else:
            raise HTTPException(status_code=400, detail=f"unknown channel '{ch}'")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("send on %s failed: %s", ch, e)
        return {"ok": False, "error": str(e)}
    get_messaging().store.add(ch, target, body, direction="out", chat_id=target,
                              importance="normal", summary=body[:80])
    return {"ok": True, "to": target}


@router.get("/announcements")
async def announcements():
    """The desktop voice listener long-polls this; we hand back any pending spoken lines."""
    return {"lines": notify.drain()}
