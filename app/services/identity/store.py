"""
Identity roster (Phase 11) — persistent, encrypted at rest.

SQLite holds the roster (name, tier, timestamps); the biometric vectors and the passphrase are
stored as **DPAPI-encrypted blobs** (see crypto.py) so nothing personal is ever on disk in the
clear. One row per enrolled person; exactly one of them is the Owner.

    add(name, tier, voice, face=None)   upsert a person
    get(name) / all() / remove(name)    read / delete
    owner()                             the Owner row (or None until enrolled)
    set_passphrase(text) / check_passphrase(text)
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass

import numpy as np

from config import IDENTITY_DB, IDENTITY_DIR
from . import crypto

_FMK = IDENTITY_DIR / ".fmk"        # local Fernet fallback key (only used if DPAPI unavailable)
_lock = threading.Lock()


def _norm(name: str) -> str:
    return (name or "").strip().lower()


@dataclass
class Identity:
    name: str                 # normalised key (lowercase)
    display: str              # how JARVIS refers to them ("Vikram")
    tier: str                 # owner | trusted | guest
    voice: np.ndarray | None  # 256-d centroid voiceprint
    face: np.ndarray | None   # 128-d centroid face embedding (optional)
    samples: int
    created: float
    updated: float


class IdentityStore:
    def __init__(self, path=IDENTITY_DB):
        self.path = path
        self._init()

    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _init(self) -> None:
        with self._con() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS identities (
                name TEXT PRIMARY KEY, display TEXT, tier TEXT,
                voice BLOB, face BLOB, samples INTEGER DEFAULT 1,
                created REAL, updated REAL)""")
            con.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v BLOB)")

    # ---- vector (de)serialisation: encrypted float32 bytes ---------------- #
    def _enc_vec(self, v: np.ndarray | None) -> bytes | None:
        if v is None:
            return None
        return crypto.protect(np.asarray(v, dtype=np.float32).tobytes(), _FMK)

    def _dec_vec(self, blob: bytes | None) -> np.ndarray | None:
        if not blob:
            return None
        try:
            return np.frombuffer(crypto.unprotect(bytes(blob), _FMK), dtype=np.float32).copy()
        except Exception:  # noqa: BLE001
            return None

    # ---- roster CRUD ------------------------------------------------------ #
    def add(self, name: str, tier: str, voice: np.ndarray | None,
            face: np.ndarray | None = None, display: str | None = None, samples: int = 1) -> None:
        key = _norm(name)
        disp = (display or name).strip()
        now = time.time()
        with _lock, self._con() as con:
            row = con.execute("SELECT created FROM identities WHERE name=?", (key,)).fetchone()
            created = row["created"] if row else now
            con.execute(
                """INSERT INTO identities (name, display, tier, voice, face, samples, created, updated)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET display=excluded.display, tier=excluded.tier,
                     voice=excluded.voice, face=excluded.face, samples=excluded.samples, updated=excluded.updated""",
                (key, disp, tier, self._enc_vec(voice), self._enc_vec(face), samples, created, now))

    def _row_to_identity(self, r: sqlite3.Row) -> Identity:
        return Identity(name=r["name"], display=r["display"], tier=r["tier"],
                        voice=self._dec_vec(r["voice"]), face=self._dec_vec(r["face"]),
                        samples=r["samples"] or 1, created=r["created"], updated=r["updated"])

    def get(self, name: str) -> Identity | None:
        with self._con() as con:
            r = con.execute("SELECT * FROM identities WHERE name=?", (_norm(name),)).fetchone()
        return self._row_to_identity(r) if r else None

    def all(self) -> list[Identity]:
        with self._con() as con:
            rows = con.execute("SELECT * FROM identities ORDER BY tier, name").fetchall()
        return [self._row_to_identity(r) for r in rows]

    def remove(self, name: str) -> bool:
        with _lock, self._con() as con:
            cur = con.execute("DELETE FROM identities WHERE name=?", (_norm(name),))
            return cur.rowcount > 0

    def owner(self) -> Identity | None:
        with self._con() as con:
            r = con.execute("SELECT * FROM identities WHERE tier='owner' LIMIT 1").fetchone()
        return self._row_to_identity(r) if r else None

    def count(self) -> int:
        with self._con() as con:
            return con.execute("SELECT COUNT(*) FROM identities").fetchone()[0]

    # ---- passphrase (encrypted; compared on normalised text) -------------- #
    @staticmethod
    def _norm_phrase(text: str) -> str:
        import re
        return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()

    def set_passphrase(self, text: str) -> None:
        val = self._norm_phrase(text)
        blob = crypto.protect(val.encode("utf-8"), _FMK) if val else b""
        with _lock, self._con() as con:
            if val:
                con.execute("INSERT INTO kv (k, v) VALUES ('passphrase', ?) "
                            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (blob,))
            else:
                con.execute("DELETE FROM kv WHERE k='passphrase'")

    def has_passphrase(self) -> bool:
        with self._con() as con:
            return con.execute("SELECT 1 FROM kv WHERE k='passphrase'").fetchone() is not None

    def check_passphrase(self, spoken: str) -> bool:
        """True if the spoken text contains the enrolled passphrase. If none is set, returns
        False (callers decide whether an unset passphrase blocks or passes)."""
        with self._con() as con:
            r = con.execute("SELECT v FROM kv WHERE k='passphrase'").fetchone()
        if not r:
            return False
        try:
            secret = crypto.unprotect(bytes(r["v"]), _FMK).decode("utf-8")
        except Exception:  # noqa: BLE001
            return False
        said = self._norm_phrase(spoken)
        return bool(secret) and secret in said


_store: IdentityStore | None = None


def get_store() -> IdentityStore:
    global _store
    if _store is None:
        _store = IdentityStore()
    return _store
