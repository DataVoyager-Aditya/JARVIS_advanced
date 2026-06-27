"""
Phase 7 — proactive notifications for new messages.

When a poller/webhook sees a noteworthy new message it calls `announce(...)`. That does two
things:
  1. broadcasts a `notify` event on the live event bus -> the PWA shows it instantly,
  2. buffers a short spoken line that the desktop voice listener drains (GET
     /messaging/announcements) and speaks aloud — "Sir, a WhatsApp from Vikram: …".

The spoken buffer is intentionally ephemeral (in-process): on restart you don't want to be
read a backlog of old pings. The messages themselves are always persisted in the store, so
nothing is lost — only the "say it out loud right now" intent is transient.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("jarvis.messaging.notify")

_buffer: list[dict] = []
_lock = threading.Lock()
_MAX = 30
_CHANNEL_LABEL = {"whatsapp": "WhatsApp", "instagram": "Instagram", "email": "email"}


async def announce(channel: str, sender: str, gist: str, *, speak: bool = True) -> None:
    """Surface a new message on every surface (HUD now, voice when the listener next polls)."""
    label = _CHANNEL_LABEL.get(channel, channel)
    line = f"Sir, a{'n' if label[0].lower() in 'aeiou' else ''} {label} message from {sender}: {gist}"
    if speak:
        with _lock:
            _buffer.append({"line": line, "ts": time.time()})
            if len(_buffer) > _MAX:
                del _buffer[:-_MAX]
    # Mirror to the PWA HUD (best-effort).
    try:
        from app.routers.events import broadcast
        await broadcast({"type": "notify", "channel": channel, "sender": sender,
                         "text": gist, "line": line})
    except Exception as e:  # noqa: BLE001
        logger.debug("HUD notify broadcast failed: %s", e)


def drain() -> list[str]:
    """Return and clear pending spoken lines (called by the voice listener's poll)."""
    with _lock:
        lines = [item["line"] for item in _buffer]
        _buffer.clear()
    return lines
