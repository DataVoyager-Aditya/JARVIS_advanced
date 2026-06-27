"""
Phase 7 — contact nickname resolution (per channel).

The boss saves people under different names on different apps (on WhatsApp "Sanskar" is saved
as "CK Deevata"; on Instagram he's "@sanskar.x"). So MY_CONTACTS.txt is split into per-channel
sections:

    [whatsapp]
    Sanskar = CK Deevata

    [instagram]
    Sanskar = sanskar.x

    [common]              # applies to every channel (used if a channel section has no match)
    Mom = Mummy

  resolve("Sanskar", "whatsapp")   -> "CK Deevata"
  resolve("Sanskar", "instagram")  -> "sanskar.x"
  display("CK Deevata", "whatsapp") -> "Sanskar"

Lookup order: the channel's own section first, then [common]. Lines before any [section]
header are treated as [common]. The file is re-read whenever it changes (no restart). Unknown
names pass through unchanged.
"""

from __future__ import annotations

import os
import re
import threading

from config import CONTACTS_PATH

_lock = threading.Lock()
_cache: dict = {"mtime": -1.0, "maps": {}}
_SEPS = ("=", "->", ":")
_COMMON = "common"


def _norm(s: str) -> str:
    """Strip everything but letters/digits + lowercase, so 'Co-Founder', 'co founder' and
    'cofounder' all match the same nickname (speech-to-text drops/adds hyphens & spaces)."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _parse(text: str) -> dict:
    """-> { channel: {'fwd': {alias_lower: real}, 'fwdnorm': {norm: real}, 'rev': {real_lower: alias}} }."""
    maps: dict = {}

    def sect(name: str) -> dict:
        return maps.setdefault(name, {"fwd": {}, "fwdnorm": {}, "rev": {}})

    section = _COMMON
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower() or _COMMON
            continue
        for sep in _SEPS:
            if sep in line:
                alias, real = line.split(sep, 1)
                alias, real = alias.strip(), real.strip()
                if alias and real:
                    s = sect(section)
                    s["fwd"][alias.lower()] = real
                    s["fwdnorm"].setdefault(_norm(alias), real)
                    s["rev"].setdefault(real.lower(), alias)
                break
    return maps


def _load() -> dict:
    try:
        mt = os.path.getmtime(CONTACTS_PATH)
    except OSError:
        return {}
    with _lock:
        if mt != _cache["mtime"]:
            try:
                maps = _parse(CONTACTS_PATH.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                maps = {}
            _cache.update(mtime=mt, maps=maps)
        return _cache["maps"]


_PREFIX = re.compile(r"^(?:my|the|a|to|our)\s+", re.IGNORECASE)


def resolve(name: str, channel: str = "", fuzzy: bool = False) -> str:
    """Nickname (as he says it) -> the real saved name on that channel (for sending/targeting).
    Checks the channel's section first, then [common]. With fuzzy=True, a final pass also matches
    a partial first name ("Om" -> "Om Tiwari Pandey") so he needn't say the whole saved name."""
    if not name:
        return name
    # The boss (and the model) often say "my sister", "the co-founder" — strip that lead-in so
    # it still matches the 'sister'/'co-founder' alias key.
    stripped = _PREFIX.sub("", name.strip()).strip() or name.strip()
    maps = _load()
    ch = (channel or "").lower()
    cands = (name, stripped) if stripped.lower() != name.strip().lower() else (name,)
    for cand in cands:
        key = cand.strip().lower()
        nkey = _norm(cand)
        # 1) exact (case-insensitive) match
        for sec in (ch, _COMMON):
            m = maps.get(sec)
            if m and key in m["fwd"]:
                return m["fwd"][key]
        # 2) punctuation/space-insensitive match ('co founder' == 'Co-Founder')
        for sec in (ch, _COMMON):
            m = maps.get(sec)
            if m and nkey and nkey in m.get("fwdnorm", {}):
                return m["fwdnorm"][nkey]
    if fuzzy:
        # 3) partial match: the spoken word is a prefix/substring of a saved alias (or vice-versa).
        # Longest alias wins so "om" prefers "Om Tiwari" over a shorter "Om" if both exist.
        for cand in cands:
            nkey = _norm(cand)
            if len(nkey) < 2:
                continue
            best = None
            for sec in (ch, _COMMON):
                for nk, real in (maps.get(sec) or {}).get("fwdnorm", {}).items():
                    if nk.startswith(nkey) or nkey in nk:
                        if best is None or len(nk) > len(best[0]):
                            best = (nk, real)
            if best:
                return best[1]
    return name


def display(real_name: str, channel: str = "") -> str:
    """Real saved name -> the nickname he uses (for showing back), per channel then [common].
    Exact match first, then contains-match, else the real name unchanged."""
    if not real_name:
        return real_name
    maps = _load()
    key = real_name.strip().lower()
    ch = (channel or "").lower()
    for sec in (ch, _COMMON):
        rev = (maps.get(sec) or {}).get("rev", {})
        if key in rev:
            return rev[key]
    for sec in (ch, _COMMON):
        rev = (maps.get(sec) or {}).get("rev", {})
        for real_key, alias in rev.items():
            if real_key in key:
                return alias
    return real_name
