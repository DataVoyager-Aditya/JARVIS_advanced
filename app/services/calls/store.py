"""
Phase 8 — the persistent call log.

Every phone-call event the Android companion reports (an incoming ring, a missed call, a call
that ended/was declined, a synced CallLog entry) is recorded here, in one SQLite table, so
"any missed calls?" is answerable even hours later or after a JARVIS restart — not an
in-memory list that dies (finality rule).

Pure-stdlib sqlite3, WAL + busy timeout (the webhook and the agent both read/write), safe to
call from worker threads. Dedup via UNIQUE(ref): the companion supplies a stable ref per call
event (CallLog id, or number+timestamp) so re-syncing the same log never duplicates rows.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from config import CALLS_DB

# Call-event kinds.
INCOMING = "incoming"     # phone is ringing right now
MISSED = "missed"         # rang out / not answered
ANSWERED = "answered"     # picked up (on the phone, or via an answer command)
DECLINED = "declined"     # rejected (by him, or via a decline command)
ENDED = "ended"           # call finished
OUTGOING = "outgoing"     # a call he placed (from a synced CallLog)


@dataclass
class Call:
    id: int
    kind: str
    number: str
    name: str          # contact display name the phone resolved (may be "" for unknown)
    ts: float
    seen: int          # 1 = JARVIS has already surfaced/announced this to him
    ref: str
    meta: dict

    @property
    def when(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.ts))

    @property
    def display(self) -> str:
        return self.name or self.number or "Unknown number"


class CallStore:
    def __init__(self, path: Path | str = CALLS_DB) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS calls (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                kind   TEXT NOT NULL,
                number TEXT NOT NULL DEFAULT '',
                name   TEXT NOT NULL DEFAULT '',
                ts     REAL NOT NULL,
                seen   INTEGER NOT NULL DEFAULT 0,
                ref    TEXT NOT NULL DEFAULT '',
                meta   TEXT NOT NULL DEFAULT '{}',
                UNIQUE(ref)
            )
        """)
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_calls_ts ON calls(ts DESC)")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_calls_kind ON calls(kind, ts DESC)")
        # Phase 8.5 — per-contact auto-handle rules the companion enforces on the phone.
        #   action: auto_text (decline + SMS a templated line) | auto_answer (accept on
        #   speaker, hands-free) | auto_decline (silent reject). `match` is the caller name or
        #   number (case-insensitive substring), `message` is the SMS body for auto_text.
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS call_rules (
                match   TEXT PRIMARY KEY,
                action  TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL
            )
        """)
        self._db.commit()

    # ---- writes ----------------------------------------------------------- #
    def add(self, kind: str, *, number: str = "", name: str = "", ts: float | None = None,
            ref: str = "", seen: int = 0, meta: dict | None = None) -> int | None:
        """Insert a call event. Returns the new row id, or None if it was a duplicate ref
        (the companion re-syncs the same CallLog every poll — dedup keeps it idempotent)."""
        ts = time.time() if ts is None else ts
        if not ref:  # a blank ref must never collide under UNIQUE(ref) — synthesize one
            ref = f"{kind}:{number}:{int(ts)}"
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO calls (kind,number,name,ts,seen,ref,meta) "
                "VALUES (?,?,?,?,?,?,?)",
                (kind, number, name, ts, int(seen), ref, json.dumps(meta or {})),
            )
            self._db.commit()
            return cur.lastrowid if cur.rowcount else None

    def mark_seen(self, *, kind: str | None = None, ids: list[int] | None = None) -> int:
        q = "UPDATE calls SET seen=1 WHERE seen=0"
        args: list = []
        if kind:
            q += " AND kind=?"; args.append(kind)
        if ids:
            q += f" AND id IN ({','.join('?' * len(ids))})"; args.extend(ids)
        with self._lock:
            cur = self._db.execute(q, args)
            self._db.commit()
            return cur.rowcount

    # ---- reads ------------------------------------------------------------ #
    def recent(self, *, kinds: tuple[str, ...] | None = None, limit: int = 10) -> list[Call]:
        q = "SELECT * FROM calls WHERE 1=1"
        args: list = []
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"; args.extend(kinds)
        # id DESC breaks ts ties so the later-inserted event is "newer" (Windows time.time()
        # is coarse, so two back-to-back events can share a timestamp).
        q += " ORDER BY ts DESC, id DESC LIMIT ?"; args.append(limit)
        return [self._row(r) for r in self._db.execute(q, args).fetchall()]

    def missed(self, *, only_unseen: bool = False, limit: int = 10) -> list[Call]:
        q = "SELECT * FROM calls WHERE kind=?"
        args: list = [MISSED]
        if only_unseen:
            q += " AND seen=0"
        q += " ORDER BY ts DESC, id DESC LIMIT ?"; args.append(limit)
        return [self._row(r) for r in self._db.execute(q, args).fetchall()]

    def missed_count(self, *, only_unseen: bool = False) -> int:
        q = "SELECT COUNT(*) FROM calls WHERE kind=?"
        args: list = [MISSED]
        if only_unseen:
            q += " AND seen=0"
        return self._db.execute(q, args).fetchone()[0]

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]

    def clear(self, *, kind: str | None = None, before: float | None = None) -> int:
        """Delete call-log rows. No filters -> wipe the whole log. `kind` limits to one kind
        (e.g. 'missed'); `before` deletes only rows older than that unix timestamp. Returns the
        number removed."""
        q = "DELETE FROM calls WHERE 1=1"
        args: list = []
        if kind:
            q += " AND kind=?"; args.append(kind)
        if before is not None:
            q += " AND ts < ?"; args.append(before)
        with self._lock:
            cur = self._db.execute(q, args)
            self._db.commit()
            return cur.rowcount

    # ---- auto-handle rules (Phase 8.5) ------------------------------------ #
    def set_rule(self, match: str, action: str, message: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO call_rules (match,action,message,updated) VALUES (?,?,?,?) "
                "ON CONFLICT(match) DO UPDATE SET action=excluded.action, "
                "message=excluded.message, updated=excluded.updated",
                (match.lower().strip(), action, message, time.time()))
            self._db.commit()

    def clear_rule(self, match: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM call_rules WHERE match=?", (match.lower().strip(),))
            self._db.commit()
            return cur.rowcount > 0

    def rules(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT match, action, message FROM call_rules ORDER BY match").fetchall()
        return [{"match": r["match"], "action": r["action"], "message": r["message"]} for r in rows]

    @staticmethod
    def _row(r: sqlite3.Row) -> Call:
        try:
            meta = json.loads(r["meta"])
        except Exception:  # noqa: BLE001
            meta = {}
        return Call(id=r["id"], kind=r["kind"], number=r["number"], name=r["name"],
                    ts=r["ts"], seen=r["seen"], ref=r["ref"], meta=meta)


_store: CallStore | None = None
_store_lock = threading.Lock()


def get_call_store() -> CallStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = CallStore()
    return _store
