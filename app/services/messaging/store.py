"""
Phase 7 — the unified message store + per-contact auto-reply rules.

Every message JARVIS sees on any channel (a WhatsApp ping pushed by the Node sidecar, an
unread email found by the poller, an Instagram DM) is recorded here, in one SQLite table,
so the "unified inbox" is a real, persistent, cross-channel surface — not an in-memory list
that dies on restart (finality rule).

Auto-reply rules ("Mom: always summarise, never auto-reply") live in the same DB so they
survive too, and are easy to read back per contact.

Pure-stdlib sqlite3, WAL + a shared busy timeout (the backend poller and the webhook both
write), safe to call from worker threads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from config import MESSAGING_DB


@dataclass
class Message:
    id: int
    channel: str          # whatsapp | instagram | email
    direction: str        # in | out
    chat_id: str          # stable per-conversation id (phone jid / ig thread / email address)
    sender: str           # raw handle/address
    sender_name: str      # friendly display name
    body: str
    ts: float             # unix seconds
    is_read: int
    importance: str       # high | normal | low | "" (unclassified)
    summary: str          # one-line LLM gist (optional)
    ref: str              # channel-native id for reply (wa message id / email Message-ID / ig item)
    muted: int = 0        # 1 = from a muted contact/group (never announced, hidden from digest)

    @property
    def when(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.ts))


class MessageStore:
    def __init__(self, path: Path | str = MESSAGING_DB) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel     TEXT NOT NULL,
                direction   TEXT NOT NULL DEFAULT 'in',
                chat_id     TEXT NOT NULL DEFAULT '',
                sender      TEXT NOT NULL DEFAULT '',
                sender_name TEXT NOT NULL DEFAULT '',
                body        TEXT NOT NULL DEFAULT '',
                ts          REAL NOT NULL,
                is_read     INTEGER NOT NULL DEFAULT 0,
                importance  TEXT NOT NULL DEFAULT '',
                summary     TEXT NOT NULL DEFAULT '',
                ref         TEXT NOT NULL DEFAULT '',
                muted       INTEGER NOT NULL DEFAULT 0,
                meta        TEXT NOT NULL DEFAULT '{}',
                UNIQUE(channel, ref)
            )
        """)
        # Migrate older DBs that predate the `muted` column.
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(messages)").fetchall()}
        if "muted" not in cols:
            self._db.execute("ALTER TABLE messages ADD COLUMN muted INTEGER NOT NULL DEFAULT 0")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_msg_ts ON messages(ts DESC)")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_msg_unread ON messages(is_read, ts DESC)")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS autoreply_rules (
                channel TEXT NOT NULL,
                contact TEXT NOT NULL,
                policy  TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL,
                PRIMARY KEY (channel, contact)
            )
        """)
        # Muted contacts/groups: messages from these are stored (so an explicit lookup still
        # finds them) but never announced and hidden from the proactive unread digest/panel.
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS mutes (
                channel TEXT NOT NULL,
                contact TEXT NOT NULL,
                created REAL NOT NULL,
                PRIMARY KEY (channel, contact)
            )
        """)
        self._db.commit()

    # ---- writes ----------------------------------------------------------- #
    def add(self, channel: str, sender_name: str, body: str, *, direction: str = "in",
            chat_id: str = "", sender: str = "", ref: str = "", ts: float | None = None,
            importance: str = "", summary: str = "", is_read: int = 0, muted: int = 0,
            meta: dict | None = None) -> int | None:
        """Insert a message. Returns the new row id, or None if it was a duplicate
        (same channel+ref already stored — pollers re-see the same unread mail every cycle)."""
        ts = time.time() if ts is None else ts
        # A blank ref must never collide under the UNIQUE(channel, ref) index, so synthesize one.
        if not ref:
            ref = f"{direction}:{int(ts*1000)}:{abs(hash((channel, sender, body))) % 10_000_000}"
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO messages "
                "(channel,direction,chat_id,sender,sender_name,body,ts,is_read,importance,summary,ref,muted,meta) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (channel, direction, chat_id, sender, sender_name, body, ts, is_read,
                 importance, summary, ref, int(muted), json.dumps(meta or {})),
            )
            self._db.commit()
            return cur.lastrowid if cur.rowcount else None

    def set_importance(self, msg_id: int, importance: str, summary: str = "") -> None:
        with self._lock:
            self._db.execute("UPDATE messages SET importance=?, summary=? WHERE id=?",
                             (importance, summary, msg_id))
            self._db.commit()

    def mark_read(self, channel: str | None = None, chat_id: str | None = None) -> int:
        q = "UPDATE messages SET is_read=1 WHERE is_read=0 AND direction='in'"
        args: list = []
        if channel:
            q += " AND channel=?"; args.append(channel)
        if chat_id:
            q += " AND chat_id=?"; args.append(chat_id)
        with self._lock:
            cur = self._db.execute(q, args)
            self._db.commit()
            return cur.rowcount

    def has_ref(self, channel: str, ref: str) -> bool:
        row = self._db.execute("SELECT 1 FROM messages WHERE channel=? AND ref=? LIMIT 1",
                               (channel, ref)).fetchone()
        return row is not None

    # ---- reads ------------------------------------------------------------ #
    def recent(self, *, channel: str | None = None, only_unread: bool = False,
               direction: str = "in", exclude_muted: bool = False,
               limit: int = 20) -> list[Message]:
        q = "SELECT * FROM messages WHERE 1=1"
        args: list = []
        if direction:
            q += " AND direction=?"; args.append(direction)
        if channel:
            q += " AND channel=?"; args.append(channel)
        if only_unread:
            q += " AND is_read=0"
        if exclude_muted:
            q += " AND muted=0"
        q += " ORDER BY ts DESC LIMIT ?"; args.append(limit)
        return [self._row(r) for r in self._db.execute(q, args).fetchall()]

    def unread_counts(self) -> dict[str, int]:
        # Muted chats never count toward the proactive unread tally.
        rows = self._db.execute(
            "SELECT channel, COUNT(*) c FROM messages WHERE is_read=0 AND direction='in' "
            "AND muted=0 GROUP BY channel").fetchall()
        return {r["channel"]: r["c"] for r in rows}

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    @staticmethod
    def _row(r: sqlite3.Row) -> Message:
        return Message(id=r["id"], channel=r["channel"], direction=r["direction"],
                       chat_id=r["chat_id"], sender=r["sender"], sender_name=r["sender_name"],
                       body=r["body"], ts=r["ts"], is_read=r["is_read"],
                       importance=r["importance"], summary=r["summary"], ref=r["ref"],
                       muted=r["muted"] if "muted" in r.keys() else 0)

    # ---- auto-reply rules ------------------------------------------------- #
    def set_rule(self, channel: str, contact: str, policy: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO autoreply_rules (channel,contact,policy,updated) VALUES (?,?,?,?) "
                "ON CONFLICT(channel,contact) DO UPDATE SET policy=excluded.policy, updated=excluded.updated",
                (channel, contact.lower(), policy, time.time()))
            self._db.commit()

    def get_rule(self, channel: str, contact: str) -> str:
        row = self._db.execute("SELECT policy FROM autoreply_rules WHERE channel=? AND contact=?",
                               (channel, contact.lower())).fetchone()
        return row["policy"] if row else ""

    def all_rules(self) -> list[tuple[str, str, str]]:
        rows = self._db.execute(
            "SELECT channel, contact, policy FROM autoreply_rules ORDER BY channel, contact").fetchall()
        return [(r["channel"], r["contact"], r["policy"]) for r in rows]

    # ---- mutes ------------------------------------------------------------ #
    def mute(self, channel: str, contact: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO mutes (channel,contact,created) VALUES (?,?,?)",
                (channel, contact.lower().strip(), time.time()))
            self._db.commit()

    def unmute(self, channel: str, contact: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM mutes WHERE channel=? AND contact=?",
                                   (channel, contact.lower().strip()))
            self._db.commit()
            return cur.rowcount > 0

    def list_mutes(self) -> list[tuple[str, str]]:
        rows = self._db.execute("SELECT channel, contact FROM mutes ORDER BY channel, contact").fetchall()
        return [(r["channel"], r["contact"]) for r in rows]

    def is_muted(self, channel: str, sender_name: str, sender: str = "") -> bool:
        """True if this sender/group matches a mute on this channel. Matching is a
        case-insensitive substring either way, so 'work group' mutes 'Work Group Project'
        and a saved name mutes its number-keyed chat."""
        return self._matches(self._db.execute(
            "SELECT contact FROM mutes WHERE channel=?", (channel,)).fetchall(),
            sender_name, sender) is not None

    @staticmethod
    def _matches(rows, sender_name: str, sender: str):
        hay = f"{sender_name} {sender}".lower()
        for r in rows:
            c = r["contact"]
            if c and (c in hay or any(c in part for part in (sender_name.lower(), sender.lower()))):
                return r
        return None


_store: MessageStore | None = None
_store_lock = threading.Lock()


def get_store() -> MessageStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = MessageStore()
    return _store
