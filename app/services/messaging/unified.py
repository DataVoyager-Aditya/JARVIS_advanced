"""
Phase 7 — the unified inbox: one ranked, cross-channel view of WhatsApp + Instagram + Email.

Reads the persisted store (every channel writes there) and returns a compact, ready-to-speak
digest ordered by importance then recency. This backs both the `unified_inbox` agent tool and
the PWA's messages surface, so the boss can ask "what's in my inbox?" and get one answer
spanning everything.
"""

from __future__ import annotations

from .store import get_store, Message

_RANK = {"high": 0, "normal": 1, "": 2, "low": 3}
_ICON = {"whatsapp": "WhatsApp", "instagram": "Instagram", "email": "Email"}


def _sort_key(m: Message):
    return (_RANK.get(m.importance, 2), -m.ts)


def unified(limit: int = 12, only_unread: bool = True) -> list[dict]:
    # Muted chats are hidden from the proactive unified view (they're still findable on demand).
    # We filter both by the stored `muted` flag AND a live is_muted() check, so muting a contact
    # hides their ALREADY-stored messages too (retroactive), not just future ones.
    store = get_store()
    msgs = store.recent(only_unread=only_unread, exclude_muted=True, limit=limit * 4)
    msgs = [m for m in msgs if not store.is_muted(m.channel, m.sender_name, m.sender)]
    msgs.sort(key=_sort_key)
    out = []
    for m in msgs[:limit]:
        out.append({
            "channel": m.channel,
            "from": m.sender_name or m.sender,
            "preview": m.summary or " ".join(m.body.split())[:120],
            "importance": m.importance or "normal",
            "when": m.when,
            "ts": m.ts,
            "chat_id": m.chat_id,
        })
    return out


def digest(limit: int = 8) -> str:
    """A short spoken-style digest of unread across all channels."""
    counts = get_store().unread_counts()
    items = unified(limit=limit, only_unread=True)
    if not items:
        return "Your inbox is clear, sir — nothing unread across WhatsApp, Instagram, or email."
    head_bits = [f"{n} {_ICON.get(ch, ch).lower()}" for ch, n in counts.items() if n]
    head = "You have " + ", ".join(head_bits) + " unread." if head_bits else ""
    lines = [head] if head else []
    for it in items:
        lines.append(f"- {_ICON.get(it['channel'], it['channel'])} — {it['from']}: {it['preview']}")
    return "\n".join(lines)
