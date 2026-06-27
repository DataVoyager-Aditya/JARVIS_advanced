"""
Phase 10.B — persistence for the intel feeds (SQLite, WAL, restart-proof).

Three tables:
  * watchlist  — what the boss asked JARVIS to watch (kind, target, label, threshold). Survives
                 restart so his watchlist is his, permanently. UNIQUE(kind,target) — re-adding just
                 updates the threshold.
  * snapshots  — the last value seen for each monitored thing (key -> json), so the monitor can spot
                 a CHANGE (a price move, a star jump) across restarts instead of re-alerting on a
                 baseline it forgot.
  * alerts     — a log of alerts fired, used for the cooldown (don't repeat the same alert within an
                 hour) and the dashboard. The spoken buffer is separate + in-memory (transient).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from config import FEEDS_DB


@dataclass
class Watch:
    id: int
    kind: str
    target: str
    label: str
    threshold: float
    meta: dict


class FeedsStore:
    def __init__(self, path: Path | str = FEEDS_DB) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                kind      TEXT NOT NULL,
                target    TEXT NOT NULL,
                label     TEXT NOT NULL DEFAULT '',
                threshold REAL NOT NULL DEFAULT 0,
                meta      TEXT NOT NULL DEFAULT '{}',
                added     REAL NOT NULL,
                UNIQUE(kind, target)
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                ts    REAL NOT NULL
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                dedup_key TEXT NOT NULL,
                line      TEXT NOT NULL,
                ts        REAL NOT NULL,
                seen      INTEGER NOT NULL DEFAULT 0
            )
        """)
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_alerts ON alerts(dedup_key, ts DESC)")
        self._db.commit()

    # ---- watchlist -------------------------------------------------------- #
    def add_watch(self, kind: str, target: str, label: str = "",
                  threshold: float = 0.0, meta: dict | None = None) -> int:
        with self._lock:
            self._db.execute(
                "INSERT INTO watchlist (kind,target,label,threshold,meta,added) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(kind,target) DO UPDATE SET label=excluded.label, "
                "threshold=excluded.threshold, meta=excluded.meta",
                (kind, target, label or target, float(threshold or 0.0),
                 json.dumps(meta or {}), time.time()))
            self._db.commit()
            row = self._db.execute("SELECT id FROM watchlist WHERE kind=? AND target=?",
                                   (kind, target)).fetchone()
            return int(row["id"]) if row else 0

    def remove_watch(self, target: str, kind: str | None = None) -> int:
        """Remove by exact TARGET first (optionally scoped by kind). Only if nothing matched the
        target do we fall back to a friendly LABEL match — and ONLY when it resolves to exactly one
        row — so a label like 'Tesla' can't silently delete an unrelated watch whose target differs
        (the old `target OR label` predicate deleted across kinds + on label collisions)."""
        kclause = " AND kind=?" if kind else ""
        kargs: list = [kind] if kind else []
        with self._lock:
            cur = self._db.execute(f"DELETE FROM watchlist WHERE target=?{kclause}", [target, *kargs])
            if cur.rowcount:
                self._db.commit()
                return cur.rowcount
            rows = self._db.execute(
                f"SELECT id FROM watchlist WHERE label=?{kclause}", [target, *kargs]).fetchall()
            if len(rows) == 1:
                self._db.execute("DELETE FROM watchlist WHERE id=?", (rows[0]["id"],))
                self._db.commit()
                return 1
            self._db.commit()
            return 0

    def watches(self, kind: str | None = None) -> list[Watch]:
        q, args = "SELECT * FROM watchlist", []
        if kind:
            q += " WHERE kind=?"
            args.append(kind)
        q += " ORDER BY kind, target"
        rows = self._db.execute(q, args).fetchall()
        out = []
        for r in rows:
            try:
                meta = json.loads(r["meta"])
            except Exception:  # noqa: BLE001
                meta = {}
            out.append(Watch(r["id"], r["kind"], r["target"], r["label"], r["threshold"], meta))
        return out

    # ---- snapshots -------------------------------------------------------- #
    def get_snapshot(self, key: str):
        row = self._db.execute("SELECT value FROM snapshots WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["value"])
        except Exception:  # noqa: BLE001
            return None

    def set_snapshot(self, key: str, value) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO snapshots (key,value,ts) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                (key, json.dumps(value), time.time()))
            self._db.commit()

    # ---- alerts (cooldown + log) ------------------------------------------ #
    def alert_recently(self, dedup_key: str, within_s: float) -> bool:
        row = self._db.execute(
            "SELECT MAX(ts) FROM alerts WHERE dedup_key=?", (dedup_key,)).fetchone()
        return bool(row and row[0] is not None and (time.time() - row[0]) < within_s)

    def record_alert(self, dedup_key: str, line: str) -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO alerts (dedup_key,line,ts) VALUES (?,?,?)",
                                   (dedup_key, line, time.time()))
            self._db.commit()
            return int(cur.lastrowid)

    def recent_alerts(self, limit: int = 10) -> list[dict]:
        rows = self._db.execute("SELECT dedup_key,line,ts FROM alerts ORDER BY ts DESC LIMIT ?",
                                (limit,)).fetchall()
        return [{"key": r["dedup_key"], "line": r["line"], "ts": r["ts"]} for r in rows]


_store: FeedsStore | None = None
_lock = threading.Lock()


def get_feeds_store() -> FeedsStore:
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                _store = FeedsStore()
    return _store
