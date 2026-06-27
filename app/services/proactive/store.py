"""
Phase 10.F — persistence for proactive intelligence.

Two jobs, both restart-proof (SQLite, WAL, shared by the backend's poll loop and the tools):
  * `fires` — a log of every self-initiated line JARVIS has spoken, so the daily cap, the minimum
    gap between lines, and the per-trigger "once today / once per 2h" dedup all survive a restart
    (an in-memory counter would reset and let him repeat himself — finality rule).
  * `settings` — a tiny key/value store for the pause/snooze switch ("stop bugging me for an hour").
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from config import PROACTIVE_DB


def _start_of_day(ts: float) -> float:
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


class ProactiveStore:
    def __init__(self, path: Path | str = PROACTIVE_DB) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS fires (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                key  TEXT NOT NULL DEFAULT '',
                ts   REAL NOT NULL
            )
        """)
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_fires_ts ON fires(ts DESC)")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                name  TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        self._db.commit()

    # ---- fires ------------------------------------------------------------ #
    def record(self, kind: str, key: str = "") -> None:
        with self._lock:
            self._db.execute("INSERT INTO fires (kind, key, ts) VALUES (?,?,?)",
                             (kind, key, time.time()))
            self._db.commit()

    def last_fire_ts(self) -> float:
        row = self._db.execute("SELECT MAX(ts) FROM fires").fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    def count_today(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return int(self._db.execute("SELECT COUNT(*) FROM fires WHERE ts >= ?",
                                    (_start_of_day(now),)).fetchone()[0])

    def fired_since(self, kind: str, key: str, since_ts: float) -> bool:
        """Has a line of this (kind, key) gone out since `since_ts`? Used for dedup — e.g. one gym
        pre-nudge per day (since = start of today), one hydration prompt per 2h (since = now-7200)."""
        row = self._db.execute(
            "SELECT 1 FROM fires WHERE kind=? AND key=? AND ts >= ? LIMIT 1",
            (kind, key, since_ts)).fetchone()
        return row is not None

    def recent(self, limit: int = 20) -> list[dict]:
        rows = self._db.execute("SELECT kind, key, ts FROM fires ORDER BY ts DESC LIMIT ?",
                                (limit,)).fetchall()
        return [{"kind": r["kind"], "key": r["key"], "ts": r["ts"]} for r in rows]

    # ---- settings (pause / snooze) ---------------------------------------- #
    def set_paused_until(self, ts: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO settings (name, value) VALUES ('paused_until', ?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (str(ts),))
            self._db.commit()

    def paused_until(self) -> float:
        row = self._db.execute("SELECT value FROM settings WHERE name='paused_until'").fetchone()
        try:
            return float(row["value"]) if row else 0.0
        except (TypeError, ValueError):
            return 0.0


_store: ProactiveStore | None = None
_store_lock = threading.Lock()


def get_proactive_store() -> ProactiveStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ProactiveStore()
    return _store
