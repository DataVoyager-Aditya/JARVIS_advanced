"""
Tier 3 — Semantic facts (durable key/value).

The canonical, deterministic things JARVIS should ALWAYS know about his boss without a
search: `user.full_name`, `user.location`, `prefs.coffee_order`, `contacts.vikram.relation`,
`routines.gym_time`, etc. These are injected straight into the system prompt every turn so
he simply *knows* them, and are updated by the `remember` tool + nightly consolidation.

Stored in `memory.db` (shared with the knowledge graph).
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("jarvis.memory.semantic")

_KEY_RE = re.compile(r"[^a-z0-9_.]+")


def slugify_key(text: str) -> str:
    k = text.strip().lower().replace(" ", "_")
    k = _KEY_RE.sub("", k).strip("._")
    return k[:60] or "fact"


@dataclass
class Fact:
    key: str
    value: str
    updated_at: str
    source: str


class SemanticStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.execute("PRAGMA busy_timeout=5000")  # backend + listener share this DB
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source     TEXT NOT NULL DEFAULT 'tool'
            )
        """)
        self._db.commit()

    def set(self, key: str, value: str, source: str = "tool") -> None:
        key = slugify_key(key)
        value = (value or "").strip()
        if not key or not value:
            return
        now = _dt.datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._db.execute(
                "INSERT INTO facts (key, value, updated_at, source) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at, source=excluded.source",
                (key, value, now, source))
            self._db.commit()
        logger.info("semantic fact set: %s = %s", key, value[:60])

    def get(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM facts WHERE key=?",
                               (slugify_key(key),)).fetchone()
        return row[0] if row else None

    def forget(self, key: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM facts WHERE key=?", (slugify_key(key),))
            self._db.commit()
        return cur.rowcount > 0

    def all(self) -> list[Fact]:
        rows = self._db.execute(
            "SELECT key, value, updated_at, source FROM facts ORDER BY key").fetchall()
        return [Fact(*r) for r in rows]

    def search(self, term: str, limit: int = 8) -> list[Fact]:
        term = (term or "").strip()
        if not term:
            return []
        rows = self._db.execute(
            "SELECT key, value, updated_at, source FROM facts "
            "WHERE key LIKE ? OR value LIKE ? ORDER BY key LIMIT ?",
            (f"%{term}%", f"%{term}%", limit)).fetchall()
        return [Fact(*r) for r in rows]

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM facts").fetchone()[0])

    def as_prompt_lines(self, limit: int = 300) -> list[str]:
        """Human-readable 'key: value' lines for system-prompt injection."""
        out = []
        for f in self.all()[:limit]:
            label = f.key.replace("_", " ").replace(".", " › ")
            out.append(f"- {label}: {f.value}")
        return out
