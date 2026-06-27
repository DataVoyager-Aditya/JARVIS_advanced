"""
Messaging tools (Phase 7) — JARVIS reads and sends across WhatsApp, Instagram, and email.

These are the agent's hands for messaging. Reading tools are free to call whenever the boss
asks; sending tools should only run after he's confirmed (the persona block enforces "never
send without his go-ahead"). Each tool degrades in character if its channel isn't connected —
it returns a plain sentence JARVIS can relay, never an exception.

Blocking work (IMAP, instagrapi, the local WhatsApp HTTP bridge) runs synchronously here,
exactly like the existing web_search tool — the agent executes tools inline.
"""

from __future__ import annotations

import logging

from config import CH_WHATSAPP, CH_INSTAGRAM, CH_EMAIL
from app.tools import tool

logger = logging.getLogger("jarvis.tools.messaging")


def _line(items: list[str], empty: str) -> str:
    return "\n".join(items) if items else empty


def _run_coro_blocking(coro):
    """Run an async coroutine to completion from a SYNC tool. Tools execute inline on the
    agent's running event loop, so we can't asyncio.run() here — instead run it on a fresh
    loop in a worker thread and block for the result."""
    import asyncio
    import threading
    box: dict = {}

    def runner():
        try:
            box["v"] = asyncio.run(coro)
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=runner)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["v"]


async def _draft_in_user_voice(channel: str, contact: str, convo: str, instruction: str,
                               their_last: str = "") -> str:
    """Draft a message the BOSS will send — in his own first-person voice (NOT as JARVIS),
    based on the recent conversation. Returns just the message text."""
    from config import JARVIS_USER_NAME
    from app.services.llm.key_rotator import get_rotator
    sys = (f"You are drafting a {channel} message that {JARVIS_USER_NAME} will send to {contact}. "
           f"Write it in {JARVIS_USER_NAME}'s OWN casual, first-person voice — as if he typed it "
           f"himself. Do NOT identify as an assistant or JARVIS. You are REPLYING to {contact}'s "
           f"most recent message, so directly address what they last said. "
           f"MATCH THE EMOTIONAL TONE of the conversation — if they're hyped, be hyped; if they're "
           f"venting or down, be warm and supportive; if it's banter/jokes, banter back; if it's "
           f"casual, stay casual. Use his real texting style (contractions, lowercase, the slang "
           f"and energy you can see in the thread). Sound like a real friend who actually cares — "
           f"never flat, formal, or robotic. Keep it short and human. "
           f"Return ONLY the message text — no quotes, no preamble, no explanation.")
    user = f"Recent conversation with {contact}:\n{convo}"
    if their_last:
        user += f"\n\n{contact}'s most recent message (reply to THIS): {their_last}"
    if instruction:
        user += f"\n\nWhat {JARVIS_USER_NAME} wants to convey: {instruction}"
    try:
        return (await get_rotator().chat(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            task="chat", temperature=0.6)).strip().strip('"')
    except Exception:  # noqa: BLE001
        return ""


# ============================ EMAIL ============================ #
@tool(
    "Read the boss's recent emails (Gmail). Use when he asks about his inbox, new mail, or a "
    "specific sender. Returns a short list of sender, subject, and a one-line gist — summarise "
    "it for him, don't read every word unless he asks.",
    params={
        "only_unread": {"type": "boolean", "description": "true = only unread mail (default), false = recent mail"},
        "limit": {"type": "number", "description": "how many to fetch (default 8)"},
    },
    narration="Checking your inbox",
)
def read_emails(only_unread: bool = True, limit: float = 8) -> str:
    from app.services.messaging.email_client import get_email, EmailError
    c = get_email()
    if not c.enabled:
        return "Email isn't connected yet — add a Gmail App Password (GMAIL_APP_PASSWORD) to switch it on."
    try:
        msgs = c.fetch_unread(int(limit)) if only_unread else c.fetch_recent(int(limit))
    except EmailError as e:
        return str(e)
    if not msgs:
        return "No unread email — your inbox is clear." if only_unread else "No recent email found."
    out = [f"{len(msgs)} {'unread' if only_unread else 'recent'} email(s):"]
    for m in msgs:
        out.append(f"- {m.display} — {m.subject}: {m.snippet[:90]}")
    return "\n".join(out)


@tool(
    "Send a NEW email from the boss's Gmail. Only call this after he has confirmed the "
    "recipient and the message. State that it was sent afterwards.",
    params={
        "to": {"type": "string", "description": "recipient email address"},
        "subject": {"type": "string", "description": "subject line"},
        "body": {"type": "string", "description": "the email body"},
    },
    required=["to", "body"],
    narration="Sending the email",
    terminal=True,
)
def send_email(to: str, body: str, subject: str = "") -> str:
    from app.services.messaging.email_client import get_email, EmailError
    from app.services.messaging.store import get_store
    c = get_email()
    if not c.enabled:
        return "Email isn't connected — add a Gmail App Password first."
    try:
        c.send(to, subject, body)
    except EmailError as e:
        return str(e)
    get_store().add(CH_EMAIL, to, body, direction="out", chat_id=to.lower(),
                    sender=c.address, importance="normal", summary=(subject or body)[:80])
    return f"Email sent to {to}."


@tool(
    "Reply to an email already in the inbox. Identify the original by sender name or subject "
    "(e.g. 'reply to Vikram' / 'reply to the invoice email'). Threads correctly. Only after "
    "the boss has confirmed the reply text.",
    params={
        "target": {"type": "string", "description": "who/which email to reply to — a sender name, address, or subject phrase"},
        "body": {"type": "string", "description": "the reply text"},
    },
    required=["target", "body"],
    narration="Sending your reply",
    terminal=True,
)
def reply_email(target: str, body: str) -> str:
    from app.services.messaging.email_client import get_email, EmailError
    from app.services.messaging.store import get_store
    c = get_email()
    if not c.enabled:
        return "Email isn't connected — add a Gmail App Password first."
    try:
        original = c.find(target)
        if not original:
            return f"I couldn't find an email matching '{target}' to reply to."
        c.reply(original, body)
    except EmailError as e:
        return str(e)
    get_store().add(CH_EMAIL, original.from_addr, body, direction="out",
                    chat_id=original.from_addr, sender=c.address, importance="normal",
                    summary=f"Re: {original.subject}"[:80])
    return f"Reply sent to {original.display}."


# ============================ WHATSAPP ============================ #
@tool(
    "The general WhatsApp INBOX — who messaged the boss recently (names + preview + unread "
    "counts). Use ONLY for 'who messaged me on WhatsApp' / 'any WhatsApp messages'. This lists "
    "only RECENT senders, so do NOT use it to read or reply to one specific person — someone he "
    "asks about may not be in the recent list. For one person's chat use read_whatsapp_chat (to "
    "read) or compose_reply (to reply based on their messages).",
    params={"limit": {"type": "number", "description": "how many chats to list (default 10)"}},
    narration="Checking WhatsApp",
)
def read_whatsapp(limit: float = 10) -> str:
    from app.services.messaging.whatsapp_client import get_whatsapp, WhatsAppError
    c = get_whatsapp()
    try:
        st = c.status_sync()
        if not st.get("ready"):
            if st.get("state") == "qr":
                return "WhatsApp is waiting to be paired — scan the QR in the sidecar terminal."
            return "WhatsApp isn't connected — start the sidecar (node sidecars/whatsapp) and scan the QR."
        msgs = c.inbox_sync(int(limit))
    except WhatsAppError as e:
        return str(e)
    incoming = [m for m in msgs if not m.get("fromMe")]
    if not incoming:
        return "No recent WhatsApp messages."
    from app.services.messaging.whatsapp_client import friendly_name
    from app.services.messaging.store import get_store
    from app.services.messaging.contacts import display as display_contact
    store = get_store()
    out, hidden = [], 0
    for m in incoming:
        raw = m.get("name") or ""
        if store.is_muted(CH_WHATSAPP, raw, m.get("chat_id", "") or ""):
            hidden += 1
            continue  # muted chat — never surface it in the general list
        flag = f" ({m['unread']} unread)" if m.get("unread") else ""
        out.append(f"- {display_contact(friendly_name(raw), CH_WHATSAPP)}{flag}: {(m.get('body') or '')[:90]}")
    if not out:
        return "No recent WhatsApp messages (everything recent is from muted chats)."
    tail = f"\n({hidden} muted chat(s) hidden.)" if hidden else ""
    return "Recent WhatsApp:\n" + "\n".join(out) + tail


@tool(
    "Read the recent messages in one specific WhatsApp conversation (a person or group). Use "
    "for 'what did Vikram say', 'open the family group', 'catch me up on the work chat'.",
    params={
        "contact": {"type": "string", "description": "contact name, group name, number, or chat id"},
        "limit": {"type": "number", "description": "how many recent messages (default 12)"},
    },
    required=["contact"],
    narration="Opening that chat",
)
def read_whatsapp_chat(contact: str, limit: float = 12) -> str:
    from app.services.messaging.whatsapp_client import get_whatsapp, WhatsAppError
    from app.services.messaging.contacts import resolve as resolve_contact
    c = get_whatsapp()
    target = resolve_contact(contact, CH_WHATSAPP)   # nickname -> real saved name
    try:
        st = c.status_sync()
        if not st.get("ready"):
            return "WhatsApp isn't connected — start the sidecar and scan the QR."
        data = c.chat_sync(target, int(limit))
    except WhatsAppError as e:
        return str(e)
    if data.get("error"):
        return f"Couldn't open that chat: {data['error']}"
    msgs = data.get("messages", [])
    if not msgs:
        return f"No messages found with {contact}."
    from app.services.messaging.whatsapp_client import friendly_name
    name = contact            # show the name HE used (his nickname)
    out = [f"Conversation with {name}:"]
    for m in msgs:
        who = "You" if m.get("fromMe") else friendly_name(m.get("from") or name)
        out.append(f"- {who}: {(m.get('body') or '')[:100]}")
    return "\n".join(out)


@tool(
    "Mark a WhatsApp conversation as read (clear its unread badge).",
    params={"contact": {"type": "string", "description": "contact/group name, number, or chat id"}},
    required=["contact"],
    narration="Marking that read",
    terminal=True,
)
def mark_whatsapp_read(contact: str) -> str:
    from app.services.messaging.whatsapp_client import get_whatsapp, WhatsAppError
    c = get_whatsapp()
    try:
        res = c.mark_read_sync(contact)
    except WhatsAppError as e:
        return str(e)
    if res.get("error"):
        return f"Couldn't mark it read: {res['error']}"
    return f"Marked your chat with {contact} as read."


@tool(
    "Delete the most recent message the boss SENT to a WhatsApp contact. for_everyone=true "
    "UNSENDS it (deletes for everyone — only works shortly after sending); for_everyone=false "
    "removes it only on his side. Use for 'delete that message I sent to X', 'unsend my last "
    "message to X'. Only when he asks.",
    params={
        "contact": {"type": "string", "description": "the contact/group the message went to"},
        "for_everyone": {"type": "boolean",
                         "description": "true = unsend for everyone (default); false = delete just for me"},
    },
    required=["contact"],
    narration="Deleting that message",
    terminal=True,
)
def delete_whatsapp_message(contact: str, for_everyone: bool = True) -> str:
    from app.services.messaging.whatsapp_client import get_whatsapp, WhatsAppError
    from app.services.messaging.contacts import resolve as resolve_contact
    c = get_whatsapp()
    target = resolve_contact(contact, CH_WHATSAPP)
    try:
        if not c.status_sync().get("ready"):
            return "WhatsApp isn't connected — start the sidecar first."
        res = c.delete_sync(target, for_everyone)
    except WhatsAppError as e:
        return str(e)
    if res.get("error"):
        if "no recent message" in res["error"]:
            return f"I don't see a recent message from you in the chat with {contact} to delete."
        return f"Couldn't delete it: {res['error']}"
    scope = "for everyone" if for_everyone else "for you only"
    return f"Deleted your last message to {contact} {scope}."


@tool(
    "Send a WhatsApp message on the boss's behalf. `to` can be a saved contact name, a phone "
    "number, or a chat id (groups work too). Only after he's confirmed. Confirm the recipient "
    "afterwards.",
    params={
        "to": {"type": "string", "description": "the EXACT word the boss used for the person — "
               "his nickname, first name, or relationship ('Farhan', 'sister', 'co-founder', 'Mom'). "
               "Do NOT substitute the person's real full name from memory; the contact book maps "
               "his word to the right saved WhatsApp name. A phone number or chat id also works."},
        "message": {"type": "string", "description": "the message text"},
    },
    required=["to", "message"],
    narration="Sending the WhatsApp",
    terminal=True,
)
def send_whatsapp(to: str, message: str) -> str:
    from app.services.messaging.whatsapp_client import get_whatsapp, WhatsAppError
    from app.services.messaging.store import get_store
    from app.services.messaging.contacts import resolve as resolve_contact
    c = get_whatsapp()
    target = resolve_contact(to, CH_WHATSAPP)        # nickname -> real saved name
    try:
        res = c.send_sync(target, message)
    except WhatsAppError as e:
        return str(e)
    if res.get("error"):
        return f"Couldn't send on WhatsApp: {res['error']}"
    get_store().add(CH_WHATSAPP, target, message, direction="out", chat_id=res.get("to", target),
                    importance="normal", summary=message[:80])
    return f"WhatsApp sent to {to}."        # confirm with the name HE used


@tool(
    "THE tool for replying to a SPECIFIC person based on their messages. It opens THAT person's "
    "own chat, reads the recent back-and-forth, and drafts a fitting reply to their last message "
    "in the boss's OWN voice (as if he wrote it, not as JARVIS). Use it whenever he says 'read X's "
    "last message and reply accordingly', 'reply to X based on what they said', 'answer X's "
    "message', 'message X about their last message'. ALWAYS use this (not read_whatsapp) for "
    "these — read_whatsapp only lists who messaged recently and will MISS anyone who isn't in the "
    "recent inbox, making you wrongly say 'no messages from them'. Returns the DRAFT only — read "
    "it back, then send with send_whatsapp / send_instagram_dm once he confirms.",
    params={
        "channel": {"type": "string", "description": "whatsapp | instagram"},
        "contact": {"type": "string", "description": "the contact/group name or @username"},
        "instruction": {"type": "string", "description": "optional steer for the reply (e.g. 'say I'll join at 6')"},
    },
    required=["channel", "contact"],
    narration="Let me pull up that chat and draft a reply — one moment, sir",
    terminal=True,
)
def compose_reply(channel: str, contact: str, instruction: str = "") -> str:
    ch = (channel or "").lower().strip()
    from app.services.messaging.contacts import resolve as resolve_contact
    target = resolve_contact(contact, ch)   # nickname -> real saved name (per channel)
    convo_lines: list[str] = []
    their_last = ""        # the most recent message FROM them — the thing to reply to
    # Pull a generous window so their last message is captured even if the boss fired off a few
    # messages after it (otherwise the recent slice can be all his own outgoing texts).
    if ch == CH_WHATSAPP:
        from app.services.messaging.whatsapp_client import get_whatsapp, WhatsAppError, friendly_name
        wc = get_whatsapp()
        try:
            if not wc.status_sync().get("ready"):
                return "WhatsApp isn't connected — start the sidecar first."
            data = wc.chat_sync(target, 18)
        except WhatsAppError as e:
            return str(e)
        if data.get("error"):
            return f"Couldn't open that chat: {data['error']}"
        for m in data.get("messages", []):
            body = (m.get("body") or "")[:200]
            if m.get("fromMe"):
                convo_lines.append(f"Me: {body}")
            else:
                convo_lines.append(f"{friendly_name(m.get('from')) or contact}: {body}")
                if body and not body.startswith("["):
                    their_last = body
    elif ch == CH_INSTAGRAM:
        from app.services.messaging.instagram import get_instagram, InstagramError
        ig = get_instagram()
        if not ig.enabled:
            return "Instagram isn't connected — add IG_USERNAME and IG_PASSWORD first."
        try:
            for who, text in ig.thread_messages(target, 14):
                convo_lines.append(f"{who}: {text[:200]}")
                if who != "Me" and text and not text.startswith("["):
                    their_last = text[:200]
        except InstagramError as e:
            return str(e)
    else:
        return "I can do this on WhatsApp or Instagram — which one?"

    if not convo_lines:
        return (f"I don't see a recent conversation with {contact} to base a reply on. "
                f"Tell me what you'd like to say and I'll send it.")
    convo = "\n".join(convo_lines[-14:])
    draft = _run_coro_blocking(_draft_in_user_voice(ch, contact, convo, instruction, their_last))
    if not draft:
        return "I couldn't draft that just now, sir — the language service is busy."
    # Tell him WHAT was read (their last message) so he can trust the draft is grounded in the
    # real conversation, then the draft, then the confirm prompt.
    ctx = f'I read your chat with {contact}. Their last message was: "{their_last}". ' if their_last else \
          f'I read your recent chat with {contact}. '
    return (f'{ctx}Here\'s a draft reply: "{draft}"\n'
            f'Say "send it" to send, or tell me what to change.')


# ============================ INSTAGRAM ============================ #
@tool(
    "Check the boss's Instagram. `kind` selects what: 'dms' = who DM'd him (unread + requests); "
    "'stories' = who he follows that has a story up right now; 'post' = likes + comments on his "
    "latest post; 'likers' = WHO liked his latest post; 'story' = who viewed his latest story; "
    "'account' = his follower/following/post counts. Summarise the result for him.",
    params={
        "kind": {"type": "string",
                 "description": "one of: dms | stories | post | likers | story | account"},
    },
    required=["kind"],
    narration="Checking Instagram",
)
def instagram_activity(kind: str = "dms") -> str:
    from app.services.messaging.instagram import get_instagram, InstagramError
    c = get_instagram()
    if not c.enabled:
        return "Instagram isn't connected — add IG_USERNAME and IG_PASSWORD to switch it on."
    kind = (kind or "dms").lower().strip()
    try:
        if kind in ("dms", "dm", "messages"):
            threads = c.unread_dms(10)
            from app.services.messaging.store import get_store
            store = get_store()
            threads = [t for t in threads
                       if not store.is_muted(CH_INSTAGRAM, t.full_name or t.title or t.username, t.username)]
            if not threads:
                return "No unread Instagram DMs."

            def _label(t):
                if t.is_group:
                    mem = f", {t.members} people" if t.members else ""
                    return f"[group] {t.title or 'Unnamed group'}{mem}"
                return f"{t.full_name or '@'+t.username} (@{t.username})"
            return "Instagram DMs:\n" + _line(
                [f"- {_label(t)}: {t.text[:80]}" for t in threads], "")
        if kind in ("stories", "tray", "feed"):
            names = c.story_tray(15)
            if not names:
                return "No one you follow has an active story right now."
            return "Stories up now from: " + ", ".join("@" + n for n in names)
        if kind in ("post", "mypost", "latest_post"):
            s = c.my_latest_post_stats()
            if not s.get("exists"):
                return "You haven't posted anything yet."
            head = f"Your latest post has {s['like_count']} likes and {s['comment_count']} comments."
            cmts = s.get("comments") or []
            if cmts:
                head += "\nRecent comments:\n" + _line(
                    [f"- @{ci['user']}: {ci['text'][:70]}" for ci in cmts[:6]], "")
            return head
        if kind in ("likers", "likes", "wholiked"):
            s = c.my_latest_post_stats(with_likers=True)
            if not s.get("exists"):
                return "You haven't posted anything yet."
            likers = s.get("likers") or []
            if not likers:
                return f"Your latest post has {s['like_count']} likes."
            shown = ", ".join("@" + u for u in likers[:15] if u)
            more = f" and {s['like_count'] - len(likers)} others" if s['like_count'] > len(likers) else ""
            return f"{s['like_count']} likes on your latest post — {shown}{more}."
        if kind in ("story", "mystory", "latest_story", "viewers"):
            s = c.my_latest_story_stats()
            if not s.get("active"):
                return "You don't have an active story right now."
            extra = (" — including " + ", ".join("@" + v for v in s["viewers"][:5])) if s.get("viewers") else ""
            return f"Your current story has {s['viewer_count']} viewers{extra}."
        if kind in ("account", "profile", "stats"):
            a = c.account_overview()
            return f"@{a['username']}: {a['followers']} followers, {a['following']} following, {a['media']} posts."
        return "I can check Instagram dms, stories, post, story, or account — which would you like?"
    except InstagramError as e:
        return str(e)


@tool(
    "Unsend (delete) the most recent Instagram DM the boss sent to someone. Use for 'delete "
    "that DM I sent to X', 'unsend my last Instagram message to X'. Instagram removes it for "
    "both sides. Only when he asks.",
    params={"username": {"type": "string", "description": "the @username he DMed"}},
    required=["username"],
    narration="Unsending that DM",
    terminal=True,
)
def delete_instagram_message(username: str) -> str:
    from app.services.messaging.instagram import get_instagram, InstagramError
    from app.services.messaging.contacts import resolve as resolve_contact
    c = get_instagram()
    if not c.enabled:
        return "Instagram isn't connected — add IG_USERNAME and IG_PASSWORD first."
    try:
        who = c.delete_last_dm(resolve_contact(username, CH_INSTAGRAM))
    except InstagramError as e:
        return str(e)
    return f"Unsent your last Instagram DM to @{who}."


@tool(
    "Send an Instagram DM on the boss's behalf to a username. Only after he's confirmed. "
    "Confirm the recipient afterwards.",
    params={
        "username": {"type": "string", "description": "the EXACT word the boss used for the person — "
                     "his nickname or relationship ('Farhan', 'sister', 'co-founder'), OR a literal "
                     "@username. Do NOT substitute the person's real name from memory; the contact "
                     "book maps his word to the right Instagram @username (different from WhatsApp)."},
        "text": {"type": "string", "description": "the message"},
    },
    required=["username", "text"],
    narration="Sending the Instagram DM",
    terminal=True,
)
def send_instagram_dm(username: str, text: str) -> str:
    from app.services.messaging.instagram import get_instagram, InstagramError
    from app.services.messaging.store import get_store
    from app.services.messaging.contacts import resolve as resolve_contact
    c = get_instagram()
    if not c.enabled:
        return "Instagram isn't connected — add IG_USERNAME and IG_PASSWORD first."
    try:
        sent_to = c.send_dm(resolve_contact(username, CH_INSTAGRAM), text)
    except InstagramError as e:
        return str(e)
    get_store().add(CH_INSTAGRAM, sent_to, text, direction="out", chat_id=sent_to,
                    importance="normal", summary=text[:80])
    return f"DM sent to @{sent_to}."


# ============================ UNIFIED / RULES ============================ #
@tool(
    "Like (or unlike) an Instagram post. `target` is a post URL or an @username (likes their "
    "latest post). Only after the boss asks. ",
    params={
        "target": {"type": "string", "description": "post URL or @username (their latest post)"},
        "unlike": {"type": "boolean", "description": "true to remove a like instead of adding one"},
    },
    required=["target"],
    narration="On Instagram",
    terminal=True,
)
def instagram_like(target: str, unlike: bool = False) -> str:
    from app.services.messaging.instagram import get_instagram, InstagramError
    c = get_instagram()
    if not c.enabled:
        return "Instagram isn't connected — add IG_USERNAME and IG_PASSWORD first."
    try:
        c.like_post(target, like=not unlike)
    except InstagramError as e:
        return str(e)
    return f"{'Unliked' if unlike else 'Liked'} {target}."


@tool(
    "Comment on an Instagram post. `target` is a post URL or @username (their latest post). "
    "Only after the boss has confirmed the comment text.",
    params={
        "target": {"type": "string", "description": "post URL or @username"},
        "text": {"type": "string", "description": "the comment"},
    },
    required=["target", "text"],
    narration="Posting your comment",
    terminal=True,
)
def instagram_comment(target: str, text: str) -> str:
    from app.services.messaging.instagram import get_instagram, InstagramError
    c = get_instagram()
    if not c.enabled:
        return "Instagram isn't connected — add IG_USERNAME and IG_PASSWORD first."
    try:
        c.comment_post(target, text)
    except InstagramError as e:
        return str(e)
    return f"Comment posted on {target}."


@tool(
    "Follow (or unfollow) an Instagram account by username. Only after the boss asks.",
    params={
        "username": {"type": "string", "description": "the @username"},
        "unfollow": {"type": "boolean", "description": "true to unfollow instead of follow"},
    },
    required=["username"],
    narration="On Instagram",
    terminal=True,
)
def instagram_follow(username: str, unfollow: bool = False) -> str:
    from app.services.messaging.instagram import get_instagram, InstagramError
    c = get_instagram()
    if not c.enabled:
        return "Instagram isn't connected — add IG_USERNAME and IG_PASSWORD first."
    try:
        u = c.follow(username, do_follow=not unfollow)
    except InstagramError as e:
        return str(e)
    return f"{'Unfollowed' if unfollow else 'Now following'} @{u}."


@tool(
    "Look up an Instagram profile by username — follower/following/post counts, bio, whether "
    "private/verified. Use for 'check out @someone' or 'how many followers does @x have'.",
    params={"username": {"type": "string", "description": "the @username"}},
    required=["username"],
    narration="Looking that up on Instagram",
)
def instagram_profile(username: str) -> str:
    from app.services.messaging.instagram import get_instagram, InstagramError
    c = get_instagram()
    if not c.enabled:
        return "Instagram isn't connected — add IG_USERNAME and IG_PASSWORD first."
    try:
        p = c.profile(username)
    except InstagramError as e:
        return str(e)
    tags = []
    if p["verified"]:
        tags.append("verified")
    if p["private"]:
        tags.append("private")
    tag = f" ({', '.join(tags)})" if tags else ""
    bio = f" — {p['bio']}" if p["bio"] else ""
    return (f"@{p['username']}{tag}: {p['followers']} followers, {p['following']} following, "
            f"{p['posts']} posts{bio}")


@tool(
    "Publish a post to the boss's Instagram feed from a local image or video file (jpg/png or "
    "mp4). Only after he's confirmed the file and caption. Confirm with the post link afterwards.",
    params={
        "file_path": {"type": "string", "description": "full path to the image/video on this PC"},
        "caption": {"type": "string", "description": "the caption (optional)"},
    },
    required=["file_path"],
    narration="Publishing your post",
    terminal=True,
)
def instagram_post(file_path: str, caption: str = "") -> str:
    from app.services.messaging.instagram import get_instagram, InstagramError
    c = get_instagram()
    if not c.enabled:
        return "Instagram isn't connected — add IG_USERNAME and IG_PASSWORD first."
    try:
        where = c.post_media(file_path, caption)
    except InstagramError as e:
        return str(e)
    return f"Posted to your Instagram feed: {where}"


@tool(
    "Add a photo or video to the boss's Instagram story from a local file (jpg/png or mp4). "
    "Only after he's confirmed. Confirm afterwards.",
    params={
        "file_path": {"type": "string", "description": "full path to the image/video on this PC"},
    },
    required=["file_path"],
    narration="Adding to your story",
    terminal=True,
)
def instagram_add_story(file_path: str) -> str:
    from app.services.messaging.instagram import get_instagram, InstagramError
    c = get_instagram()
    if not c.enabled:
        return "Instagram isn't connected — add IG_USERNAME and IG_PASSWORD first."
    try:
        c.add_story(file_path)
    except InstagramError as e:
        return str(e)
    return "Added to your Instagram story."


@tool(
    "Give the boss one combined view of everything unread across WhatsApp, Instagram, and "
    "email — ranked by importance. Use for 'what's in my inbox', 'any new messages', 'catch me up'.",
    params={},
    narration="Pulling your unified inbox",
)
def unified_inbox() -> str:
    # Refresh live channels FIRST so the digest matches reality even on a cold start (the
    # background poller may not have run yet). Email is the pull channel that most needs this;
    # WhatsApp arrives via push and Instagram via its poller. Best-effort — never block on it.
    try:
        from app.services.messaging.pollers import refresh_inbox
        _run_coro_blocking(refresh_inbox())
    except Exception:  # noqa: BLE001
        pass
    from app.services.messaging.unified import digest
    return digest(8)


@tool(
    "Reply to the boss's UNREAD messages on a channel, on his behalf — ONLY when he explicitly "
    "asks ('reply to my WhatsApp messages', 'answer my Instagram DMs'). JARVIS reads each unread "
    "conversation and sends an appropriate reply, ALWAYS identifying himself as the boss's AI "
    "assistant (never pretending to be him). Optionally narrow to one contact, and/or give an "
    "instruction ('tell them I'll call tomorrow'). NEVER call this on your own initiative — only "
    "when he asks. Report back who you replied to.",
    params={
        "channel": {"type": "string", "description": "whatsapp | instagram"},
        "instruction": {"type": "string",
                        "description": "optional steer for all the replies, e.g. 'say I'm busy and will reply tonight'"},
        "contact": {"type": "string",
                    "description": "optional: only reply to this one contact/group (partial name ok)"},
    },
    required=["channel"],
    narration="Replying to your messages",
    terminal=True,
)
def reply_to_messages(channel: str, instruction: str = "", contact: str = "") -> str:
    ch = channel.lower().strip()
    if ch not in (CH_WHATSAPP, CH_INSTAGRAM):
        return "I can reply on WhatsApp or Instagram — which one?"
    if ch == CH_WHATSAPP:
        from app.services.messaging.whatsapp_client import get_whatsapp
        if not get_whatsapp().status_sync().get("ready"):
            return "WhatsApp isn't connected — start the sidecar and scan the QR first."
    else:
        from app.services.messaging.instagram import get_instagram
        if not get_instagram().enabled:
            return "Instagram isn't connected — add IG_USERNAME and IG_PASSWORD first."
    from app.services.messaging.autoreply import reply_to_unread
    res = _run_coro_blocking(reply_to_unread(ch, instruction=instruction, contact_filter=contact))
    if res.get("error"):
        return res["error"]
    if res["count"] == 0:
        scope = f" from {contact}" if contact else ""
        return f"No unread {ch} messages{scope} to reply to."
    out = f"Replied to {res['count']} {ch} conversation(s): " + ", ".join(res["sent"]) + "."
    if res.get("failed"):
        out += " Couldn't reach: " + ", ".join(res["failed"]) + "."
    return out


@tool(
    "Store a freeform handling note for a contact ('Mom: always notify me, she's important'). "
    "A memory note for how the boss likes someone handled — it does NOT make JARVIS reply on "
    "his own (replies only happen when he asks, via reply_to_messages).",
    params={
        "channel": {"type": "string", "description": "whatsapp | instagram | email"},
        "contact": {"type": "string", "description": "the contact name/handle the note is about"},
        "policy": {"type": "string", "description": "the note in plain words"},
    },
    required=["channel", "contact", "policy"],
    narration="Noting that",
    terminal=True,
)
def set_autoreply_rule(channel: str, contact: str, policy: str) -> str:
    from app.services.messaging.store import get_store
    ch = channel.lower().strip()
    if ch not in (CH_WHATSAPP, CH_INSTAGRAM, CH_EMAIL):
        return "Which channel — WhatsApp, Instagram, or email?"
    get_store().set_rule(ch, contact, policy)
    return f"Noted — for {contact} on {ch}: {policy}"


@tool(
    "Mute a contact or group on a channel. JARVIS will still keep their messages, but will "
    "NEVER announce them and will hide them from the inbox digest — total silence on them "
    "until the boss unmutes. Use for 'mute the family group on WhatsApp', 'stop telling me "
    "about @brand on Instagram'.",
    params={
        "channel": {"type": "string", "description": "whatsapp | instagram | email"},
        "contact": {"type": "string", "description": "the contact or group name/handle to mute (a partial name is fine)"},
    },
    required=["channel", "contact"],
    narration="Muting that",
    terminal=True,
)
def mute_chat(channel: str, contact: str) -> str:
    from app.services.messaging.store import get_store
    from app.services.messaging.contacts import resolve as resolve_contact
    ch = channel.lower().strip()
    if ch not in (CH_WHATSAPP, CH_INSTAGRAM, CH_EMAIL):
        return "Which channel — WhatsApp, Instagram, or email?"
    get_store().mute(ch, resolve_contact(contact, ch))   # mute the real saved name
    return f"Muted {contact} on {ch}. You won't hear from them until you unmute."


@tool(
    "Unmute a previously muted contact or group, so JARVIS announces them again.",
    params={
        "channel": {"type": "string", "description": "whatsapp | instagram | email"},
        "contact": {"type": "string", "description": "the contact or group to unmute"},
    },
    required=["channel", "contact"],
    narration="Unmuting that",
    terminal=True,
)
def unmute_chat(channel: str, contact: str) -> str:
    from app.services.messaging.store import get_store
    from app.services.messaging.contacts import resolve as resolve_contact
    ch = channel.lower().strip()
    ok = get_store().unmute(ch, resolve_contact(contact, ch))
    return (f"Unmuted {contact} on {ch}." if ok
            else f"{contact} wasn't on the muted list for {ch}.")


@tool(
    "List who the boss currently has muted across all channels. Use for 'who have I muted'.",
    params={},
)
def list_muted() -> str:
    from app.services.messaging.store import get_store
    mutes = get_store().list_mutes()
    if not mutes:
        return "Nothing is muted — you'll hear about everyone."
    return "Muted: " + "; ".join(f"{c} ({ch})" for ch, c in mutes)


@tool(
    "Check which messaging channels are currently connected (WhatsApp, Instagram, email) and "
    "the unread counts. Use when the boss asks if something is set up or working.",
    params={},
)
def messaging_status() -> str:
    # Lightweight: WhatsApp = ask the sidecar (fast HTTP); Instagram/email = is it configured
    # (we don't force a blocking IG login just to answer "is it set up?").
    from app.services.messaging.email_client import get_email
    from app.services.messaging.whatsapp_client import get_whatsapp
    from app.services.messaging.instagram import get_instagram
    from app.services.messaging.store import get_store
    wa = get_whatsapp().status_sync()
    wa_state = "connected" if wa.get("ready") else (
        "waiting for QR pairing" if wa.get("state") == "qr" else "not connected")
    ig_state = "configured" if get_instagram().enabled else "not connected"
    em_state = "connected" if get_email().enabled else "not connected"
    unread = get_store().unread_counts()
    tail = ("  Unread — " + ", ".join(f"{k}: {v}" for k, v in unread.items())) if unread else ""
    return f"WhatsApp: {wa_state}; Instagram: {ig_state}; email: {em_state}.{tail}"
