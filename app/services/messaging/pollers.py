"""
Phase 7 — background pollers for the channels that aren't push-based.

WhatsApp pushes new messages to our webhook (the sidecar does the watching), so it needs no
poller here. Email and Instagram are pull, so we poll them gently:

  - email     : every EMAIL_POLL_SECONDS (default 5 min), find UNSEEN mail, classify, store,
                and announce anything important.
  - instagram : every IG_POLL_SECONDS (default 15 min, jittered), check unread DMs, store,
                announce. Kept slow + jittered to stay under the radar (ban-safety).

Each cycle is fully guarded: a transient network/login error logs and the loop simply tries
again next interval. The blocking IMAP/instagrapi work runs in a thread so the event loop is
never blocked.
"""

from __future__ import annotations

import asyncio
import logging

from config import (CH_EMAIL, CH_INSTAGRAM, EMAIL_POLL_SECONDS, IG_POLL_SECONDS)
from .store import get_store
from .classifier import classify
from . import notify

logger = logging.getLogger("jarvis.messaging.poll")


async def _process_incoming(channel: str, *, sender_name: str, sender: str, body: str,
                            subject: str = "", chat_id: str = "", ref: str = "",
                            use_llm: bool = True, announce: bool = True) -> None:
    """Classify + store + (if important) announce a single inbound message. Deduped by ref.
    use_llm=False uses the fast free heuristic (for on-demand refreshes — no rate budget hit)."""
    store = get_store()
    if ref and store.has_ref(channel, ref):
        return
    muted = store.is_muted(channel, sender_name or sender, sender)
    if use_llm:
        importance, summary = await classify(sender_name or sender, body, subject=subject,
                                             channel=channel)
    else:
        from .classifier import heuristic_only
        importance, summary = heuristic_only(sender_name or sender, subject, body)
    new_id = store.add(channel, sender_name or sender, body, chat_id=chat_id, sender=sender,
                       ref=ref, importance=importance, summary=summary, muted=1 if muted else 0,
                       meta={"subject": subject} if subject else None)
    if new_id is None:
        return  # duplicate raced in
    # Muted contacts/groups are stored but NEVER announced — JARVIS stays quiet on them.
    # NOTE: JARVIS NEVER replies on his own. Replying happens only when the boss explicitly
    # asks ("reply to my WhatsApp messages") via the reply_to_messages tool. No background bot.
    if importance == "high" and not muted and announce:
        await notify.announce(channel, sender_name or sender, summary)


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
async def email_poll_once(use_llm: bool = True) -> int:
    from .email_client import get_email
    client = get_email()
    if not client.enabled:
        return 0
    msgs = await asyncio.to_thread(client.fetch_unread, 15)
    n = 0
    for m in msgs:
        before = get_store().count()
        await _process_incoming(CH_EMAIL, sender_name=m.from_name, sender=m.from_addr,
                                body=m.snippet or m.body, subject=m.subject,
                                chat_id=m.from_addr, ref=m.message_id or m.uid, use_llm=use_llm)
        if get_store().count() > before:
            n += 1
    if n:
        logger.info("email poll: %d new unread", n)
    return n


async def _email_loop() -> None:
    from .email_client import get_email
    if not get_email().enabled:
        logger.info("email poller idle — no GMAIL_APP_PASSWORD set")
        return
    logger.info("email poller started (every %ds)", EMAIL_POLL_SECONDS)
    while True:
        try:
            await email_poll_once()
        except Exception as e:  # noqa: BLE001
            logger.warning("email poll failed: %s", e)
        await asyncio.sleep(EMAIL_POLL_SECONDS)


# --------------------------------------------------------------------------- #
# Instagram
# --------------------------------------------------------------------------- #
async def instagram_poll_once() -> int:
    from .instagram import get_instagram, InstagramError
    client = get_instagram()
    if not client.enabled:
        return 0
    try:
        threads = await asyncio.to_thread(client.unread_dms, 10)
    except InstagramError as e:
        logger.warning("instagram poll skipped: %s", e)
        return 0
    n = 0
    for t in threads:
        before = get_store().count()
        await _process_incoming(CH_INSTAGRAM, sender_name=t.full_name or t.username,
                                sender=t.username, body=t.text, chat_id=t.thread_id,
                                ref=f"{t.thread_id}:{t.ts:.0f}")
        if get_store().count() > before:
            n += 1
    if n:
        logger.info("instagram poll: %d new DMs", n)
    return n


async def _instagram_loop() -> None:
    from config import IG_AUTOPOLL
    from .instagram import get_instagram
    if not IG_AUTOPOLL:
        logger.info("instagram background polling is OFF (on-demand only) — set IG_AUTOPOLL=1 to enable")
        return
    if not get_instagram().enabled:
        logger.info("instagram poller idle — no IG_USERNAME/IG_PASSWORD set")
        return
    logger.info("instagram poller started (every ~%ds)", IG_POLL_SECONDS)
    # small startup delay so login doesn't fire in the same instant as everything else
    await asyncio.sleep(20)
    while True:
        try:
            await instagram_poll_once()
        except Exception as e:  # noqa: BLE001
            logger.warning("instagram poll failed: %s", e)
        # jitter ±20% to look human
        import random
        await asyncio.sleep(IG_POLL_SECONDS * (0.8 + 0.4 * random.random()))


# --------------------------------------------------------------------------- #
# WhatsApp (push-based, but we can also pull the current inbox on demand)
# --------------------------------------------------------------------------- #
async def whatsapp_pull_once(use_llm: bool = True) -> int:
    """Pull the current WhatsApp inbox into the store (for the on-demand unified view).
    WhatsApp normally pushes via the webhook; this fills the store on a fresh start."""
    from .whatsapp_client import get_whatsapp, WhatsAppError
    c = get_whatsapp()
    try:
        st = await c.status()
        if not st.get("ready"):
            return 0
        chats = await c.inbox(20)
    except WhatsAppError:
        return 0
    from .whatsapp_client import friendly_name
    n = 0
    for m in chats:
        if m.get("fromMe") or not m.get("body"):
            continue
        before = get_store().count()
        await _process_incoming(CH_WHATSAPP, sender_name=friendly_name(m.get("name")), sender="",
                                body=m.get("body", ""), chat_id=m.get("chat_id", ""),
                                ref=f"{m.get('chat_id','')}:{int(m.get('ts',0))}", use_llm=use_llm)
        if get_store().count() > before:
            n += 1
    return n


async def refresh_inbox() -> None:
    """On-demand: pull the live pull-channels (email + WhatsApp) into the store so the unified
    inbox is current the instant the boss asks. Uses the FREE heuristic (use_llm=False) so a
    casual 'what's in my inbox' never spends a stack of LLM calls / rate budget."""
    import asyncio as _a
    results = await _a.gather(email_poll_once(use_llm=False), whatsapp_pull_once(use_llm=False),
                             return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            logger.debug("refresh_inbox sub-pull failed: %s", r)


# --------------------------------------------------------------------------- #
_tasks: list[asyncio.Task] = []


def start_pollers() -> None:
    """Launch the email + instagram poll loops on the running event loop (idempotent)."""
    global _tasks
    if _tasks:
        return
    _tasks = [asyncio.create_task(_email_loop()), asyncio.create_task(_instagram_loop())]
    logger.info("messaging pollers launched")
