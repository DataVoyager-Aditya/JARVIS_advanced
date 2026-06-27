"""
Phase 10.A — persistence for deep research (SQLite, WAL, restart-proof).

  * briefings — every synthesized briefing JARVIS produced: the short spoken digest, the full
                structured markdown, the sources, a confidence note, and a `signature` (a compact
                fingerprint of the key facts) used to detect a MATERIAL change on a re-run. Kept so
                "what did you find on X" / "read me that briefing again" work across restarts, and
                so a digest can be surfaced into long-term memory.
  * monitors  — topics he asked JARVIS to KEEP WATCHING: re-run cadence + when it last ran + the
                last signature, so the scheduler re-researches on time and only alerts on change.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from config import RESEARCH_DB


_KEEP_PER_TOPIC = 10   # retain only the latest N briefings per topic (no unbounded growth)


def topic_key(topic: str) -> str:
    """Normalize a topic to a stable lookup key (lowercase, collapsed, trimmed, length-capped)."""
    return re.sub(r"\s+", " ", (topic or "").strip().lower())[:200]


def _like_escape(s: str) -> str:
    r"""Escape the SQL LIKE metacharacters so a topic containing % or _ (e.g. '100% renewable',
    'gpt_4') is matched LITERALLY, not as a wildcard. Pairs with `ESCAPE '\'` on the query."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass
class Briefing:
    id: int
    topic: str          # the normalized key
    title: str          # human phrasing as the boss said it
    summary: str        # short, ready-to-speak digest
    full_md: str        # the full structured briefing
    sources: list
    confidence: str
    signature: str
    ts: float


@dataclass
class Monitor:
    topic: str          # normalized key
    label: str          # human phrasing
    interval_h: float
    last_run_ts: float
    last_signature: str
    added: float


class ResearchStore:
    def __init__(self, path: Path | str = RESEARCH_DB) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS briefings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                topic      TEXT NOT NULL,
                title      TEXT NOT NULL DEFAULT '',
                summary    TEXT NOT NULL DEFAULT '',
                full_md    TEXT NOT NULL DEFAULT '',
                sources    TEXT NOT NULL DEFAULT '[]',
                confidence TEXT NOT NULL DEFAULT '',
                signature  TEXT NOT NULL DEFAULT '',
                ts         REAL NOT NULL
            )
        """)
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_brief_topic ON briefings(topic, ts DESC)")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS monitors (
                topic          TEXT PRIMARY KEY,
                label          TEXT NOT NULL DEFAULT '',
                interval_h     REAL NOT NULL DEFAULT 24,
                last_run_ts    REAL NOT NULL DEFAULT 0,
                last_signature TEXT NOT NULL DEFAULT '',
                added          REAL NOT NULL
            )
        """)
        self._db.commit()

    # ---- briefings -------------------------------------------------------- #
    def save_briefing(self, topic: str, title: str, summary: str, full_md: str,
                      sources: list, confidence: str, signature: str) -> int:
        key = topic_key(topic)
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO briefings (topic,title,summary,full_md,sources,confidence,signature,ts) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (key, title or topic, summary, full_md, json.dumps(sources or []),
                 confidence, signature, time.time()))
            # Retention: keep only the latest N briefings for this topic so the table can't grow
            # without bound (a daily monitor that keeps detecting change would append forever).
            self._db.execute(
                "DELETE FROM briefings WHERE topic=? AND id NOT IN "
                "(SELECT id FROM briefings WHERE topic=? ORDER BY ts DESC LIMIT ?)",
                (key, key, _KEEP_PER_TOPIC))
            self._db.commit()
            return int(cur.lastrowid)

    def _row_to_briefing(self, r) -> Briefing:
        try:
            sources = json.loads(r["sources"])
        except Exception:  # noqa: BLE001
            sources = []
        return Briefing(r["id"], r["topic"], r["title"], r["summary"], r["full_md"],
                        sources, r["confidence"], r["signature"], r["ts"])

    def latest_briefing(self) -> Briefing | None:
        with self._lock:   # the shared connection is used from both the worker + backend threads
            r = self._db.execute("SELECT * FROM briefings ORDER BY ts DESC LIMIT 1").fetchone()
        return self._row_to_briefing(r) if r else None

    def get_briefing(self, topic: str) -> Briefing | None:
        """Most recent briefing for a topic: exact key first, then a fuzzy LIKE on the key/title."""
        key = topic_key(topic)
        if not key:
            return self.latest_briefing()
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM briefings WHERE topic=? ORDER BY ts DESC LIMIT 1", (key,)).fetchone()
            if r:
                return self._row_to_briefing(r)
            like = f"%{_like_escape(key)}%"
            r = self._db.execute(
                "SELECT * FROM briefings WHERE topic LIKE ? ESCAPE '\\' OR lower(title) LIKE ? ESCAPE '\\' "
                "ORDER BY ts DESC LIMIT 1", (like, like)).fetchone()
        return self._row_to_briefing(r) if r else None

    def list_briefings(self, limit: int = 12) -> list[Briefing]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM briefings ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_briefing(r) for r in rows]

    # ---- monitors --------------------------------------------------------- #
    def add_monitor(self, topic: str, label: str, interval_h: float) -> None:
        key = topic_key(topic)
        with self._lock:
            self._db.execute(
                "INSERT INTO monitors (topic,label,interval_h,last_run_ts,last_signature,added) "
                "VALUES (?,?,?,0,'',?) "
                "ON CONFLICT(topic) DO UPDATE SET label=excluded.label, interval_h=excluded.interval_h",
                (key, label or topic, float(interval_h), time.time()))
            self._db.commit()

    def remove_monitor(self, topic: str) -> int:
        key = topic_key(topic)
        with self._lock:
            cur = self._db.execute("DELETE FROM monitors WHERE topic=?", (key,))
            if not cur.rowcount:   # fall back to a fuzzy match on key/label (wildcards escaped so a
                like = f"%{_like_escape(key)}%"   # topic with % or _ can't delete an unrelated row)
                rows = self._db.execute(
                    "SELECT topic FROM monitors WHERE topic LIKE ? ESCAPE '\\' "
                    "OR lower(label) LIKE ? ESCAPE '\\'", (like, like)).fetchall()
                if len(rows) == 1:
                    cur = self._db.execute("DELETE FROM monitors WHERE topic=?", (rows[0]["topic"],))
            self._db.commit()
            return cur.rowcount

    def monitors(self) -> list[Monitor]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM monitors ORDER BY added").fetchall()
        return [Monitor(r["topic"], r["label"], r["interval_h"], r["last_run_ts"],
                        r["last_signature"], r["added"]) for r in rows]

    def due_monitors(self, now: float | None = None) -> list[Monitor]:
        now = time.time() if now is None else now
        return [m for m in self.monitors()
                if now - m.last_run_ts >= m.interval_h * 3600.0]

    def mark_monitor_run(self, topic: str, signature: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self._db.execute(
                "UPDATE monitors SET last_run_ts=?, last_signature=? WHERE topic=?",
                (now, signature, topic_key(topic)))
            self._db.commit()


_store: ResearchStore | None = None
_store_lock = threading.Lock()


def get_research_store() -> ResearchStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ResearchStore()
    return _store
