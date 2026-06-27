"""
Knowledge graph — entities + relations (triples).

The nightly consolidator extracts triples from the day's conversation
(`aditya` —[works_on]→ `project_atlas`, `vikram` —[is]→ `brother`) and stores them here.
This lets JARVIS answer "tell me about project Atlas / Vikram" structurally, by pulling
every relation touching that entity, without a fuzzy vector search.

Stored in `memory.db` (shared with semantic facts).
"""

from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("jarvis.memory.graph")


@dataclass
class Triple:
    subject: str
    predicate: str
    obj: str
    updated_at: str


def _norm(name: str) -> str:
    return (name or "").strip().lower()


class KnowledgeGraph:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.execute("PRAGMA busy_timeout=5000")  # backend + listener share this DB
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                name    TEXT PRIMARY KEY,
                type    TEXT NOT NULL DEFAULT 'thing',
                summary TEXT
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS relations (
                subject    TEXT NOT NULL,
                predicate  TEXT NOT NULL,
                object     TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (subject, predicate, object)
            )
        """)
        self._db.commit()

    def upsert_entity(self, name: str, type_: str = "thing", summary: str | None = None) -> None:
        name = _norm(name)
        if not name:
            return
        with self._lock:
            if summary:
                self._db.execute(
                    "INSERT INTO entities (name, type, summary) VALUES (?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET type=excluded.type, summary=excluded.summary",
                    (name, type_, summary))
            else:
                self._db.execute(
                    "INSERT INTO entities (name, type) VALUES (?,?) "
                    "ON CONFLICT(name) DO UPDATE SET type=excluded.type",
                    (name, type_))
            self._db.commit()

    def add_triple(self, subject: str, predicate: str, obj: str) -> None:
        subject, obj = _norm(subject), _norm(obj)
        predicate = (predicate or "").strip().lower().replace(" ", "_")
        if not (subject and predicate and obj):
            return
        now = _dt.datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._db.execute(
                "INSERT INTO relations (subject, predicate, object, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(subject, predicate, object) DO UPDATE SET updated_at=excluded.updated_at",
                (subject, predicate, obj, now))
            for n in (subject, obj):
                self._db.execute("INSERT OR IGNORE INTO entities (name) VALUES (?)", (n,))
            self._db.commit()
        logger.info("triple: %s -%s-> %s", subject, predicate, obj)

    def about(self, entity: str, limit: int = 20) -> list[Triple]:
        e = _norm(entity)
        if not e:
            return []
        rows = self._db.execute(
            "SELECT subject, predicate, object, updated_at FROM relations "
            "WHERE subject=? OR object=? ORDER BY updated_at DESC LIMIT ?",
            (e, e, limit)).fetchall()
        return [Triple(*r) for r in rows]

    def describe(self, entity: str) -> str:
        """A natural-language-ish dump of everything known about an entity."""
        e = _norm(entity)
        ent = self._db.execute("SELECT type, summary FROM entities WHERE name=?", (e,)).fetchone()
        triples = self.about(e)
        if not ent and not triples:
            return ""
        lines = []
        if ent and ent[1]:
            lines.append(ent[1])
        for t in triples:
            if t.subject == e:
                lines.append(f"{t.subject} {t.predicate.replace('_', ' ')} {t.obj}")
            else:
                lines.append(f"{t.subject} {t.predicate.replace('_', ' ')} {t.obj}")
        return "; ".join(lines)

    def ego_graph(self, limit: int = 5) -> tuple[str, list[str]]:
        """The most-connected entity + its neighbours — for the HUD memory-graph view."""
        center = self._db.execute(
            "SELECT name, COUNT(*) c FROM "
            "(SELECT subject AS name FROM relations UNION ALL SELECT object AS name FROM relations) "
            "GROUP BY name ORDER BY c DESC LIMIT 1").fetchone()
        if not center:
            return ("", [])
        c = center[0]
        rows = self._db.execute(
            "SELECT object FROM relations WHERE subject=? "
            "UNION SELECT subject FROM relations WHERE object=? LIMIT ?",
            (c, c, limit)).fetchall()
        return (c, [r[0] for r in rows])

    def counts(self) -> tuple[int, int]:
        e = int(self._db.execute("SELECT COUNT(*) FROM entities").fetchone()[0])
        r = int(self._db.execute("SELECT COUNT(*) FROM relations").fetchone()[0])
        return e, r
