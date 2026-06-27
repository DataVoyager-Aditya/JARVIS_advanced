"""
Tier 2 — Episodic memory (FAISS vector store + SQLite metadata).

Every conversational turn, explicit `remember`, and nightly day-summary is stored here as
a row with metadata (kind, role, channel, timestamp, mood, entities) AND a vector. Recall
is semantic: encode the query, inner-product search the FAISS index, return the nearest
records (optionally filtered by recency / channel).

Design:
  - SQLite `episodes` table is the source of truth (text + metadata). rowid is the vector id.
  - FAISS `IndexIDMap(IndexFlatIP)` over 384-dim normalized vectors → cosine similarity.
  - The FAISS file is a rebuildable cache: if it's missing/corrupt, we re-encode from SQLite.
  - If embeddings are unavailable, search degrades to SQLite lexical LIKE — never hard-fails.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .embeddings import get_embedder, EMBED_DIM

logger = logging.getLogger("jarvis.memory.episodic")


@dataclass
class Episode:
    id: int
    text: str
    kind: str            # "turn" | "fact" | "summary"
    role: str            # "user" | "assistant" | "memory" | "system"
    channel: str
    ts: float            # epoch seconds
    when: str            # human ISO string
    mood: str | None
    entities: list[str]
    score: float = 0.0   # similarity at recall time


class EpisodicStore:
    def __init__(self, db_path: Path, index_path: Path) -> None:
        self.db_path = Path(db_path)
        self.index_path = Path(index_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.execute("PRAGMA busy_timeout=5000")  # backend + listener share this DB
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                text      TEXT NOT NULL,
                kind      TEXT NOT NULL DEFAULT 'turn',
                role      TEXT NOT NULL DEFAULT 'user',
                channel   TEXT NOT NULL DEFAULT 'pc_voice',
                ts        REAL NOT NULL,
                mood      TEXT,
                entities  TEXT NOT NULL DEFAULT '[]'
            )
        """)
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes(ts)")
        self._db.commit()
        self._embed = get_embedder()
        self._index = None
        self._load_index()

    # ---- FAISS index lifecycle -------------------------------------------- #
    def _new_index(self):
        import faiss
        return faiss.IndexIDMap2(faiss.IndexFlatIP(EMBED_DIM))

    def _load_index(self) -> None:
        if not self._embed.available():
            return  # lexical-only mode; no vector index
        try:
            import faiss
            if self.index_path.exists():
                self._index = faiss.read_index(str(self.index_path))
                if self._index.d != EMBED_DIM:
                    raise ValueError("index dim mismatch")
            else:
                self._index = self._new_index()
                self._rebuild_locked()
        except Exception as e:  # noqa: BLE001
            logger.warning("FAISS index load failed (%s) — rebuilding from SQLite", e)
            try:
                self._index = self._new_index()
                self._rebuild_locked()
            except Exception as e2:  # noqa: BLE001
                logger.warning("FAISS rebuild failed (%s) — lexical fallback only", e2)
                self._index = None

    def _rebuild_locked(self) -> None:
        rows = self._db.execute("SELECT id, text FROM episodes ORDER BY id").fetchall()
        if not rows:
            self._persist_index()
            return
        ids = np.array([r[0] for r in rows], dtype=np.int64)
        vecs = self._embed.encode([r[1] for r in rows])
        self._index.add_with_ids(vecs, ids)
        self._persist_index()
        logger.info("episodic index rebuilt: %d vectors", len(rows))

    def _persist_index(self) -> None:
        if self._index is None:
            return
        import faiss
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self._index, str(self.index_path))

    # ---- write ------------------------------------------------------------ #
    def add(self, text: str, *, kind: str = "turn", role: str = "user",
            channel: str = "pc_voice", mood: str | None = None,
            entities: list[str] | None = None, ts: float | None = None) -> int:
        text = (text or "").strip()
        if not text:
            return -1
        ts = ts if ts is not None else _dt.datetime.now().timestamp()
        ent = json.dumps(entities or [])
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO episodes (text, kind, role, channel, ts, mood, entities) "
                "VALUES (?,?,?,?,?,?,?)",
                (text, kind, role, channel, ts, mood, ent),
            )
            self._db.commit()
            rid = int(cur.lastrowid)
            if self._index is not None:
                try:
                    vec = self._embed.encode([text])
                    self._index.add_with_ids(vec, np.array([rid], dtype=np.int64))
                    self._persist_index()
                except Exception as e:  # noqa: BLE001
                    logger.warning("vector add failed for episode %d (%s)", rid, e)
        return rid

    # ---- read ------------------------------------------------------------- #
    def _row_to_episode(self, row: tuple, score: float = 0.0) -> Episode:
        _id, text, kind, role, channel, ts, mood, entities = row
        try:
            ent = json.loads(entities) if entities else []
        except json.JSONDecodeError:
            ent = []
        when = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        return Episode(_id, text, kind, role, channel, ts, when, mood, ent, score)

    # Floor calibrated for all-MiniLM-L6-v2: asymmetric query→statement cosines run low
    # (a clearly relevant hit lands ~0.18-0.5, unrelated chatter ~0.03-0.08), so 0.15
    # admits real matches while keeping noise out. Ranking does the rest.
    def search(self, query: str, k: int = 5, *, since: float | None = None,
               channel: str | None = None, min_score: float = 0.15) -> list[Episode]:
        query = (query or "").strip()
        if not query:
            return []
        if self._index is not None and self._index.ntotal > 0:
            return self._search_vector(query, k, since, channel, min_score)
        return self._search_lexical(query, k, since, channel)

    def _search_vector(self, query, k, since, channel, min_score) -> list[Episode]:
        try:
            qv = self._embed.encode([query])
        except Exception:  # noqa: BLE001
            return self._search_lexical(query, k, since, channel)
        # over-fetch so post-filtering (recency/channel/score) still yields k.
        fetch = min(self._index.ntotal, max(k * 4, 20))
        scores, ids = self._index.search(qv, fetch)
        out: list[Episode] = []
        for score, rid in zip(scores[0], ids[0]):
            if rid < 0 or score < min_score:
                continue
            row = self._db.execute(
                "SELECT id,text,kind,role,channel,ts,mood,entities FROM episodes WHERE id=?",
                (int(rid),)).fetchone()
            if not row:
                continue
            ep = self._row_to_episode(row, float(score))
            if since is not None and ep.ts < since:
                continue
            if channel is not None and ep.channel != channel:
                continue
            out.append(ep)
            if len(out) >= k:
                break
        return out

    def _search_lexical(self, query, k, since, channel) -> list[Episode]:
        sql = "SELECT id,text,kind,role,channel,ts,mood,entities FROM episodes WHERE text LIKE ?"
        params: list = [f"%{query}%"]
        if since is not None:
            sql += " AND ts >= ?"; params.append(since)
        if channel is not None:
            sql += " AND channel = ?"; params.append(channel)
        sql += " ORDER BY ts DESC LIMIT ?"; params.append(k)
        rows = self._db.execute(sql, params).fetchall()
        return [self._row_to_episode(r) for r in rows]

    def recent(self, hours: float = 24.0, *, kinds: tuple[str, ...] = ("turn",)) -> list[Episode]:
        since = _dt.datetime.now().timestamp() - hours * 3600
        placeholders = ",".join("?" for _ in kinds)
        rows = self._db.execute(
            f"SELECT id,text,kind,role,channel,ts,mood,entities FROM episodes "
            f"WHERE ts >= ? AND kind IN ({placeholders}) ORDER BY ts",
            (since, *kinds)).fetchall()
        return [self._row_to_episode(r) for r in rows]

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
