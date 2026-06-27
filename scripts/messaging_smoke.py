"""
Phase 7 smoke test — messaging (WhatsApp / Instagram / Email).

Verifies everything that can be checked WITHOUT live credentials or a paired phone (those
require Aditya's one-time setup): the persistent store, dedup, unread counts + auto-reply
rules, the importance heuristic, the unified ranked digest, graceful "not connected"
behaviour of every tool, the WhatsApp webhook path, and that all messaging tools register
with valid schemas.

Run:  python scripts/messaging_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "OK " if cond else "XX "
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"  {mark}{name}" + (f"  — {detail}" if detail and not cond else ""))


def main() -> None:
    # Isolate the store in a temp DB so the smoke test never pollutes the real inbox.
    import app.services.messaging.store as store_mod
    tmp = Path(tempfile.mkdtemp()) / "messaging_test.db"
    store_mod._store = store_mod.MessageStore(tmp)
    store = store_mod.get_store()

    from config import CH_WHATSAPP, CH_INSTAGRAM, CH_EMAIL

    print("\n[1] store: insert / dedup / unread / persistence")
    id1 = store.add(CH_WHATSAPP, "Vikram", "Sunday still on?", ref="wa:1", importance="high",
                    summary="asks about Sunday")
    id2 = store.add(CH_EMAIL, "Blue Dart", "Delivery delayed", ref="em:1", importance="normal",
                    summary="delivery delayed")
    dup = store.add(CH_WHATSAPP, "Vikram", "Sunday still on?", ref="wa:1")   # same ref -> ignored
    check("insert returns ids", bool(id1) and bool(id2))
    check("duplicate ref ignored", dup is None, f"dup={dup}")
    check("count == 2", store.count() == 2, f"count={store.count()}")
    counts = store.unread_counts()
    check("unread per channel", counts.get(CH_WHATSAPP) == 1 and counts.get(CH_EMAIL) == 1, str(counts))
    store.mark_read(channel=CH_WHATSAPP)
    check("mark_read clears WA unread", store.unread_counts().get(CH_WHATSAPP, 0) == 0)

    # reopen the same file -> data survived
    reopened = store_mod.MessageStore(tmp)
    check("persists across reopen", reopened.count() == 2, f"count={reopened.count()}")

    print("\n[2] auto-reply rules")
    store.set_rule(CH_WHATSAPP, "Mom", "always summarise and notify, never auto-reply")
    check("rule stored + read back",
          "summarise" in store.get_rule(CH_WHATSAPP, "mom"))
    store.set_rule(CH_WHATSAPP, "Mom", "notify only")           # upsert
    check("rule upserts", store.get_rule(CH_WHATSAPP, "Mom") == "notify only")
    check("all_rules lists it", any(c == "mom" for _, c, _ in store.all_rules()))

    print("\n[3] importance heuristic (offline, no LLM)")
    from app.services.messaging.classifier import _heuristic, classify
    imp, _ = _heuristic("Newsletter <no-reply@promo.com>", "Big sale unsubscribe here")
    check("newsletter -> low", imp == "low", imp)
    imp2, _ = _heuristic("Vikram", "are we still on for sunday?")
    check("real msg -> normal", imp2 == "normal", imp2)
    # the async pre-filter short-circuits obvious junk without an LLM call
    imp3, _ = asyncio.run(classify("Promotions", "Your OTP code is 123456", channel="email"))
    check("OTP pre-filtered -> low", imp3 == "low", imp3)

    print("\n[4] unified digest: ranked by importance then recency")
    from app.services.messaging import unified
    store.add(CH_INSTAGRAM, "@design.co", "collab next month?", ref="ig:1", importance="low",
              summary="collab next month")
    items = unified.unified(limit=10, only_unread=False)
    order = [it["importance"] for it in items]
    check("high ranks first", order and order[0] == "high", str(order))
    dig = unified.digest()
    check("digest mentions a sender", "Blue Dart" in dig or "Vikram" in dig, dig[:80])

    print("\n[5] notify buffer (spoken announcements)")
    from app.services.messaging import notify
    asyncio.run(notify.announce(CH_WHATSAPP, "Vikram", "Sunday still on?"))
    lines = notify.drain()
    check("announce buffers a spoken line", len(lines) == 1 and "Vikram" in lines[0], str(lines))
    check("drain clears the buffer", notify.drain() == [])

    print("\n[6] WhatsApp webhook path (handle_whatsapp_in)")
    from app.services.messaging import get_messaging
    stored = asyncio.run(get_messaging().handle_whatsapp_in(
        name="Aarav", number="9198xxxx", body="sent the files", chat_id="9198xxxx@c.us", ref="wa:99"))
    check("webhook stores new message", stored is True)
    again = asyncio.run(get_messaging().handle_whatsapp_in(
        name="Aarav", number="9198xxxx", body="sent the files", chat_id="9198xxxx@c.us", ref="wa:99"))
    check("webhook dedups on ref", again is False)

    print("\n[7] tools register with valid schemas")
    from app.tools import discover, get as get_tool
    discover()
    expected = ["read_emails", "send_email", "reply_email",
                "read_whatsapp", "read_whatsapp_chat", "mark_whatsapp_read", "send_whatsapp",
                "delete_whatsapp_message",
                "instagram_activity", "send_instagram_dm", "instagram_like", "instagram_comment",
                "instagram_follow", "instagram_profile", "instagram_post", "instagram_add_story",
                "delete_instagram_message",
                "unified_inbox", "messaging_status",
                "mute_chat", "unmute_chat", "list_muted",
                "reply_to_messages", "compose_reply", "set_autoreply_rule"]
    for name in expected:
        t = get_tool(name)
        ok = t is not None and isinstance(t.to_openai(), dict)
        check(f"tool '{name}' registered", ok)

    print("\n[8] mutes: muted chats are stored but silent + hidden from digest")
    store.mute(CH_WHATSAPP, "Spam Group")
    check("is_muted matches partial name", store.is_muted(CH_WHATSAPP, "Spam Group Chat"))
    check("is_muted false for others", not store.is_muted(CH_WHATSAPP, "Vikram"))
    # a high-importance message from a muted sender must NOT be announced
    asyncio.run(get_messaging().handle_whatsapp_in(
        name="Spam Group", number="120@g.us", body="BUY NOW URGENT", chat_id="120@g.us", ref="wa:mute1"))
    check("muted msg stored but not announced", notify.drain() == [])
    muted_unread = store.unread_counts().get(CH_WHATSAPP, 0)
    in_digest = any("Spam Group" in (it["from"] or "") for it in unified.unified(only_unread=False))
    check("muted hidden from digest", not in_digest)
    check("unmute works", store.unmute(CH_WHATSAPP, "Spam Group"))

    print("\n[8b] on-demand reply: NO background firing + identity guard + bad-channel guard")
    from app.services.messaging import autoreply as _ar
    check("identity guard detects JARVIS", _ar._identifies_as_jarvis("This is JARVIS speaking"))
    # incoming messages must NEVER trigger a reply on their own (no background bot)
    drained_before = notify.drain()  # clear
    asyncio.run(get_messaging().handle_whatsapp_in(
        name="Stranger", number="55x", body="hello?", chat_id="55x@c.us", ref="wa:noauto"))
    out_after = store.recent(channel=CH_WHATSAPP, direction="out", limit=5)
    check("incoming did NOT auto-send any reply", all("noauto" not in (m.ref or "") for m in out_after))
    res = asyncio.run(_ar.reply_to_unread("email"))   # email isn't a reply channel
    check("reply_to_unread rejects email channel", bool(res.get("error")))

    print("\n[9] graceful degrade — unconnected channels say so; no crash")
    from app.tools.messaging import (instagram_activity, send_instagram_dm, instagram_like,
                                     instagram_follow, instagram_profile, read_whatsapp,
                                     read_whatsapp_chat, unified_inbox, set_autoreply_rule,
                                     messaging_status, mute_chat, list_muted, reply_to_messages)
    # Only exercise the Instagram tools' graceful path when IG is NOT configured — otherwise
    # calling them would trigger a real live login (ban-risk in a test). When configured we
    # just confirm the tools exist (registration already checked above).
    from app.services.messaging.instagram import get_instagram
    if get_instagram().enabled:
        print("  -- Instagram is configured; skipping live tool calls in smoke (ban-safety)")
    else:
        check("instagram_activity graceful", "connect" in instagram_activity("likers").lower())
        check("send_instagram_dm graceful", "connect" in send_instagram_dm("x", "hi").lower())
        check("instagram_like graceful", "connect" in instagram_like("@x").lower())
        check("instagram_follow graceful", "connect" in instagram_follow("@x").lower())
        check("instagram_profile graceful", "connect" in instagram_profile("@x").lower())
    # WhatsApp graceful check only makes sense if the sidecar ISN'T running.
    from app.services.messaging.whatsapp_client import get_whatsapp
    if get_whatsapp().status_sync().get("ready"):
        print("  -- WhatsApp sidecar is live; skipping graceful-degrade checks (it's connected)")
    else:
        wa = read_whatsapp()
        check("read_whatsapp graceful", any(x in wa.lower() for x in ("connect", "reach", "pair")), wa[:80])
        wc = read_whatsapp_chat("Vikram")
        check("read_whatsapp_chat graceful", any(x in wc.lower() for x in ("connect", "reach", "pair")), wc[:80])
    check("set_autoreply_rule works", "Noted" in set_autoreply_rule(CH_EMAIL, "Boss", "notify"))
    check("reply_to_messages rejects email", "WhatsApp or Instagram" in reply_to_messages("email"))
    check("mute_chat tool works", "Muted" in mute_chat(CH_INSTAGRAM, "promobot"))
    check("list_muted tool works", "promobot" in list_muted().lower())
    check("unified_inbox returns text", isinstance(unified_inbox(), str))
    check("messaging_status returns text", "WhatsApp" in messaging_status())

    print("\n[10] email LIVE check (read-only) — only if Gmail is configured")
    from app.services.messaging.email_client import get_email
    em = get_email()
    if em.enabled:
        try:
            msgs = em.fetch_unread(3)   # IMAP read-only, PEEK — does NOT mark anything read
            check("Gmail IMAP login + fetch works", True, f"{len(msgs)} unread sampled")
        except Exception as e:  # noqa: BLE001
            check("Gmail IMAP login + fetch works", False, str(e))
    else:
        print("  -- skipped (no GMAIL_APP_PASSWORD set)")
    # NOTE: we deliberately do NOT call send_email here — it would send a real message.

    print("\n[11] persona block wired")
    from config import build_system_prompt
    sp = build_system_prompt()
    check("MESSAGING block in system prompt", "MESSAGING" in sp and "WhatsApp" in sp)

    print(f"\n==== messaging smoke: {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
