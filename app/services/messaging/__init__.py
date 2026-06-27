"""
Phase 7 — JARVIS's messaging layer (WhatsApp · Instagram · Email).

One persistent, cross-channel inbox plus the ability to send on the boss's behalf — all on
free transports:
  - WhatsApp  : local whatsapp-web.js Node sidecar (QR once, session persists); pushes
                incoming messages to our webhook.
  - Instagram : instagrapi (unofficial, free), session persisted to disk; gentle polling.
  - Email     : Gmail IMAP read + SMTP send via a free App Password (no OAuth/Cloud project).

Public surface:
  get_messaging()                       -> MessagingService facade
  MessagingService.status()             -> per-channel connection state
  MessagingService.handle_whatsapp_in() -> webhook entrypoint for the sidecar
  pollers.start_pollers()               -> background email + IG polling
  unified.digest()/unified.unified()    -> the merged inbox

Every channel degrades gracefully: missing creds / sidecar down => the tool reports it in
character; never a crash, never a paid path.
"""

from __future__ import annotations

import logging

from config import CH_WHATSAPP, CH_INSTAGRAM, CH_EMAIL
from .store import get_store
from . import unified, notify

logger = logging.getLogger("jarvis.messaging")


class MessagingService:
    def __init__(self) -> None:
        self.store = get_store()

    # ---- status ----------------------------------------------------------- #
    async def status(self) -> dict:
        from .email_client import get_email
        from .whatsapp_client import get_whatsapp
        from .instagram import get_instagram
        import asyncio

        wa = await get_whatsapp().status()
        email = get_email()
        ig = await asyncio.to_thread(get_instagram().status)
        counts = self.store.unread_counts()
        return {
            CH_WHATSAPP: {"connected": bool(wa.get("ready")), "detail": wa},
            CH_INSTAGRAM: {"connected": bool(ig.get("connected")), "detail": ig},
            CH_EMAIL: {"connected": email.enabled,
                       "detail": {"address": email.address} if email.enabled else {"reason": "no app password"}},
            "unread": counts,
        }

    # ---- incoming (WhatsApp webhook) -------------------------------------- #
    async def handle_whatsapp_in(self, *, name: str, number: str, body: str,
                                 chat_id: str = "", group: str = "", is_group: bool = False,
                                 ref: str = "") -> bool:
        """Called by routers.messaging when the sidecar pushes a new WhatsApp message.
        Classifies, stores, and announces. Returns True if it was newly stored."""
        from .pollers import _process_incoming
        from .whatsapp_client import friendly_name
        # Group chats are high-volume chatter — classify them with the FREE heuristic (no LLM
        # call per message, which was flooding the API and burning the rate limit) and never
        # announce them. Only direct 1-on-1 messages get the LLM classifier + announcement.
        is_group = bool(is_group) or (chat_id or "").endswith("@g.us")
        member = friendly_name(name or number)
        if is_group and group:
            # Store the GROUP as the conversation identity so muting it by name works (a member's
            # name can't match a group mute), and keep WHO sent it in the preview body.
            sender_name = group
            body = f"{member}: {body}" if member and member != "an unknown number" else body
        else:
            sender_name = member
        before = self.store.count()
        await _process_incoming(CH_WHATSAPP, sender_name=sender_name,
                                sender=number, body=body, chat_id=chat_id or number, ref=ref,
                                use_llm=not is_group, announce=not is_group)
        return self.store.count() > before


_service: MessagingService | None = None


def get_messaging() -> MessagingService:
    global _service
    if _service is None:
        _service = MessagingService()
    return _service


__all__ = ["MessagingService", "get_messaging", "unified", "notify"]
