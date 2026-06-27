"""
Phase 10.F — the proactive engine: decides WHEN JARVIS speaks up on his own, and supplies the line.

The voice listener polls `poll(state)` every ~25 s with its current state (in a conversation? how
long since the boss last said anything?). The engine evaluates a small set of triggers in priority
order and, if one is due AND passes the gate, returns a line to speak. Otherwise it returns nothing
— silence is the default. A returned line is NOT counted until the listener confirms it was actually
spoken via `record(kind, key)` (the /proactive/ack path) — so a dropped/timed-out line never burns
the daily cap or arms the min-gap.

Triggers (priority high→low):
  1. routine pre-nudge   — 30–60 min before a `routines.*` block ("gym in 45 min, queue your playlist?")
  2. call gap            — "it's been ~N days since you and Mom spoke" (real call log, answered/outgoing only)
  3. hydration / break   — after ~90 min heads-down talking to him ("worth a water break?")
  4. routine check-in    — 20–90 min after a routine, sometimes ("how'd the gym go?")
  5. idle chatter        — a quiet lull mid-conversation: an LLM-composed, context-aware remark (or <SILENT>)

Gate: disabled/paused, the active user isn't the Owner, the daily cap is hit, too soon since the last
line, his mood says hold back (vulnerable → silence everything; frustrated/urgent → only the routine
pre-nudge), or quiet hours (23:00–08:00). Quiet hours suppress the UNSOLICITED triggers (call gap,
hydration, idle) but NOT the routine nudges — a routine he explicitly scheduled is wanted even early.
Everything is jittered + coin-flipped so it never feels scripted, and counted fires persist so the
cap/gap/dedup survive a restart. Routines honour a recurrence qualifier (weekdays/weekends/daily/day
names) and wrap correctly around midnight.
"""

from __future__ import annotations

import logging
import random
import re
import time

from config import (
    JARVIS_USER_NAME,
    MEMORY_DB,
    PROACTIVE_ENABLED,
    PROACTIVE_QUIET_START,
    PROACTIVE_QUIET_END,
    PROACTIVE_DAILY_CAP,
    PROACTIVE_MIN_GAP_S,
    PROACTIVE_IDLE_MIN_S,
    PROACTIVE_IDLE_MAX_S,
    PROACTIVE_SESSION_GAP_S,
    PROACTIVE_LONG_SESSION_S,
    PROACTIVE_CALL_GAP_DAYS,
    PROACTIVE_IDLE_PROB,
    PROACTIVE_POSTEVENT_PROB,
)
from app.services.memory.semantic import SemanticStore
from app.services.proactive.store import ProactiveStore, get_proactive_store, _start_of_day

logger = logging.getLogger("jarvis.proactive")

_NONE = {"say": None, "kind": None, "key": None, "expects_reply": False}

# Registers that hold JARVIS back. vulnerable -> total silence; frustrated/urgent -> only the brief
# routine pre-nudge (genuinely useful, not chatter) is allowed.
_SILENCE_ALL = {"vulnerable"}
_CHATTER_BLOCK = {"vulnerable", "frustrated", "urgent"}

# Time parsing. AM/PM and 24h both use .search (unanchored) so a trailing qualifier like
# "7:00 am on weekdays" / "18:00 daily" still parses. The 24h match forbids surrounding digits so it
# doesn't grab two of a four-digit "0730".
_TIME_AMPM = re.compile(r"(\d{1,2})(?:[:.\s](\d{2}))?\s*([ap])\.?m\.?", re.I)
_TIME_24 = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")

_WEEKDAYS = {0, 1, 2, 3, 4}      # Mon–Fri (tm_wday: Mon=0 … Sun=6)
_WEEKENDS = {5, 6}
_DAY_TOKENS = [("mon",), ("tue",), ("wed",), ("thu",), ("fri",), ("sat",), ("sun",)]


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    """Parse a routine time string into (hour, minute). Tolerates a trailing qualifier and accepts
    '18:00', '6pm', '6:30 pm', '7', '0730'/'1830' (military), '18:00 daily', '7am on weekdays'."""
    v = (value or "").strip().lower()
    m = _TIME_AMPM.search(v)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
        if ap == "p" and h != 12:
            h += 12
        if ap == "a" and h == 12:
            h = 0
        if 0 <= h < 24 and 0 <= mi < 60:
            return h, mi
    m = _TIME_24.search(v)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h < 24 and 0 <= mi < 60:
            return h, mi
    # 3–4 digit military time ("730" -> 7:30, "1830" -> 18:30)
    digits = re.match(r"^(\d{3,4})\b", v)
    if digits:
        d = digits.group(1)
        h, mi = int(d[:-2]), int(d[-2:])
        if 0 <= h < 24 and 0 <= mi < 60:
            return h, mi
    if v.isdigit() and 0 <= int(v) < 24:           # bare hour
        return int(v), 0
    return None


def _parse_days(value: str) -> set[int] | None:
    """Parse a recurrence qualifier from a routine value. Returns a set of weekday ints the routine
    runs on, or None for 'every day' (the default when nothing is said / 'daily')."""
    v = (value or "").lower()
    if "weekend" in v:
        return set(_WEEKENDS)
    if "weekday" in v:
        return set(_WEEKDAYS)
    days = {i for i, toks in enumerate(_DAY_TOKENS) if any(t in v for t in toks)}
    return days or None


def _label(routine_key: str) -> str:
    """'gym_time' -> 'gym', 'evening_walk' -> 'evening walk'."""
    n = routine_key[:-5] if routine_key.endswith("_time") else routine_key
    return n.replace("_", " ").strip() or "routine"


class ProactiveEngine:
    def __init__(self, store: ProactiveStore | None = None,
                 semantic: SemanticStore | None = None,
                 rng: random.Random | None = None) -> None:
        self._store = store or get_proactive_store()
        # Read facts directly (not via the heavy MemoryService) so the engine stays light and the
        # smoke doesn't load embeddings. Shares memory.db with busy_timeout.
        self._semantic = semantic if semantic is not None else SemanticStore(MEMORY_DB)
        self._rng = rng or random.Random()
        self._active_since: float | None = None   # start of the current heads-down work streak

    # ------------------------------------------------------------------ #
    # The poll entry point
    # ------------------------------------------------------------------ #
    async def poll(self, state: dict) -> dict:
        now = time.time()
        self._track_session(now, state)
        if not self._hard_ok(now):                 # disabled/paused/not-owner/cap/min-gap
            return dict(_NONE)
        reg = self._register()
        if reg in _SILENCE_ALL:
            return dict(_NONE)
        chatty_ok = reg not in _CHATTER_BLOCK
        quiet = self._quiet_hours(now)

        # Routine pre-nudge: tied to a routine he explicitly scheduled, so it bypasses BOTH quiet
        # hours and the frustrated/urgent mood gate (brief, useful, wanted).
        cand = self._routine_pre(now)
        # Unsolicited triggers: suppressed in quiet hours and when his mood says hold back.
        if not cand and chatty_ok and not quiet:
            cand = self._call_gap(now) or self._hydration(now)
        # Post check-in: also tied to an explicit routine (bypasses quiet) but respects mood.
        if not cand and chatty_ok:
            cand = self._routine_post(now)

        if cand:
            logger.info("proactive: %s — %s", cand["kind"], cand["line"])
            return {"say": cand["line"], "kind": cand["kind"], "key": cand["key"],
                    "expects_reply": cand.get("expects_reply", True)}

        # Idle chatter last (an LLM compose; may decide there's nothing worth saying).
        if chatty_ok and not quiet and self._idle_eligible(now, state):
            line = await self._compose_idle()
            if line:
                logger.info("proactive: idle — %s", line)
                return {"say": line, "kind": "idle", "key": f"idle:{int(now)}", "expects_reply": False}
        return dict(_NONE)

    def record(self, kind: str, key: str = "") -> None:
        """Count a line as actually spoken (called from /proactive/ack). This — not poll() — is what
        drives the daily cap, the min-gap and per-trigger dedup, so a line that never reached the
        speakers can't suppress later ones."""
        self._store.record(kind, key)

    # ------------------------------------------------------------------ #
    # Gate
    # ------------------------------------------------------------------ #
    def _hard_ok(self, now: float) -> bool:
        if not PROACTIVE_ENABLED:
            return False
        if self._store.paused_until() > now:
            return False
        if not self._owner_active():
            return False
        if self._store.count_today(now) >= PROACTIVE_DAILY_CAP:
            return False
        if now - self._store.last_fire_ts() < PROACTIVE_MIN_GAP_S:
            return False
        return True

    def _quiet_hours(self, now: float) -> bool:
        h = time.localtime(now).tm_hour
        s, e = PROACTIVE_QUIET_START, PROACTIVE_QUIET_END
        if s == e:
            return False
        return (s <= h < e) if s < e else (h >= s or h < e)   # latter wraps midnight (23..8)

    def _register(self) -> str:
        try:
            from app.services import emotion
            return emotion.snapshot().get("register", "neutral")
        except Exception:  # noqa: BLE001
            return "neutral"

    def _owner_active(self) -> bool:
        try:
            from app.services import identity
            if not identity.is_enrolled():     # open mode — everyone is treated as Owner
                return True
            return bool(identity.get_active().is_owner)
        except Exception:  # noqa: BLE001
            return True

    # ------------------------------------------------------------------ #
    # Session tracking (for the hydration / long-session nudge)
    # ------------------------------------------------------------------ #
    def _track_session(self, now: float, state: dict) -> None:
        idle = float(state.get("idle_s", 1e9))
        if idle <= PROACTIVE_SESSION_GAP_S:        # interacted recently => still heads-down
            if self._active_since is None:
                self._active_since = now
        else:                                       # a long gap broke the streak
            self._active_since = None

    # ------------------------------------------------------------------ #
    # Routines (from semantic memory: routines.<name> = "<time> [recurrence]";
    #           routines.<name>.notify = false opts out)
    # ------------------------------------------------------------------ #
    def _routines(self) -> list[tuple[str, int, int, set[int] | None]]:
        times: dict[str, tuple[int, int]] = {}
        recur: dict[str, set[int] | None] = {}
        notify: dict[str, bool] = {}
        try:
            facts = self._semantic.all()
        except Exception:  # noqa: BLE001
            return []
        for f in facts:
            if not f.key.startswith("routines."):
                continue
            sub = f.key[len("routines."):]
            if sub.endswith(".notify"):
                notify[sub[:-len(".notify")]] = (f.value or "").strip().lower() not in (
                    "false", "0", "off", "no")
                continue
            if "." in sub:                          # some other option key — ignore
                continue
            t = _parse_hhmm(f.value)
            if t:
                times[sub] = t
                recur[sub] = _parse_days(f.value)
        return [(name, h, mi, recur.get(name))
                for name, (h, mi) in times.items() if notify.get(name, True)]

    def _event_candidates(self, now: float, h: int, mi: int) -> list[float]:
        """The clock time h:mi on yesterday/today/tomorrow, so pre/post windows wrap around midnight
        (mktime normalises an over/under-flowed mday)."""
        lt = time.localtime(now)
        return [time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + d, h, mi, 0, 0, 0, -1))
                for d in (-1, 0, 1)]

    @staticmethod
    def _day_ok(ev_ts: float, days: set[int] | None) -> bool:
        return days is None or time.localtime(ev_ts).tm_wday in days

    def _routine_pre(self, now: float) -> dict | None:
        for name, h, mi, days in self._routines():
            for ev in self._event_candidates(now, h, mi):
                delta = ev - now
                if 30 * 60 <= delta <= 60 * 60 and self._day_ok(ev, days):
                    key = f"pre:{name}"
                    if self._store.fired_since("routine_pre", key, _start_of_day(now)):
                        break
                    label, mins = _label(name), int(round(delta / 60))
                    line = f"Heads up, sir — your {label} block is in about {mins} minutes."
                    if "gym" in label or "workout" in label:
                        line += " Want me to queue your playlist?"
                    return {"kind": "routine_pre", "key": key, "line": line, "expects_reply": True}
        return None

    def _routine_post(self, now: float) -> dict | None:
        for name, h, mi, days in self._routines():
            for ev in self._event_candidates(now, h, mi):
                delta = now - ev
                if 20 * 60 <= delta <= 90 * 60 and self._day_ok(ev, days):
                    key = f"post:{name}"
                    if self._store.fired_since("routine_post", key, _start_of_day(now)):
                        break
                    if self._rng.random() >= PROACTIVE_POSTEVENT_PROB:
                        break
                    line = f"How'd the {_label(name)} go, sir?"
                    return {"kind": "routine_post", "key": key, "line": line, "expects_reply": True}
        return None

    # ------------------------------------------------------------------ #
    # Call gap (real Phase-8 call log; ANSWERED/OUTGOING only = an actual conversation)
    # ------------------------------------------------------------------ #
    def _call_gap(self, now: float) -> dict | None:
        try:
            from app.services.calls.store import get_call_store, ANSWERED, OUTGOING
            from app.services.messaging import contacts
            from config import CH_CALL
            rows = get_call_store().recent(kinds=(ANSWERED, OUTGOING), limit=200)
        except Exception:  # noqa: BLE001
            return None
        last: dict[str, float] = {}
        for c in rows:
            # Prefer the number->relationship rule; fall back to the phone-supplied name (mirrors
            # calls._nice_name) so phone-known and space-separated caller-ids still resolve.
            rel = ""
            try:
                if c.number:
                    rel = contacts.display(c.number, CH_CALL)
                if (not rel or rel == c.number) and c.name:
                    rel = contacts.display(c.name, CH_CALL) or c.name
            except Exception:  # noqa: BLE001
                continue
            if not rel or rel == c.number or rel.lower() in ("unknown number", "unknown"):
                continue
            last[rel] = max(last.get(rel, 0.0), c.ts)
        best: tuple[str, str, float] | None = None
        for rel, ts in last.items():
            age = now - ts
            if PROACTIVE_CALL_GAP_DAYS * 86400 <= age <= 120 * 86400:
                key = f"callgap:{rel}"
                if self._store.fired_since("call_gap", key, _start_of_day(now)):
                    continue
                if best is None or age > best[2]:
                    best = (rel, key, age)
        if best:
            rel, key, age = best
            line = (f"It's been about {int(age // 86400)} days since you and {rel} spoke, sir — "
                    f"want to give {rel} a ring?")
            return {"kind": "call_gap", "key": key, "line": line, "expects_reply": True}
        return None

    # ------------------------------------------------------------------ #
    # Hydration / long-session break
    # ------------------------------------------------------------------ #
    def _hydration(self, now: float) -> dict | None:
        if self._active_since is None:
            return None
        dur = now - self._active_since
        if dur < PROACTIVE_LONG_SESSION_S:
            return None
        if self._store.fired_since("hydration", "hydration", now - 7200):
            return None
        line = (f"You've been heads-down about {int(dur // 60)} minutes, sir — "
                f"worth a quick water break?")
        return {"kind": "hydration", "key": "hydration", "line": line, "expects_reply": False}

    # ------------------------------------------------------------------ #
    # Idle chatter (LLM-composed, context-aware; may return None = stay silent)
    # ------------------------------------------------------------------ #
    def _idle_eligible(self, now: float, state: dict) -> bool:
        if not state.get("in_conversation"):
            return False
        idle = float(state.get("idle_s", 0))
        if not (PROACTIVE_IDLE_MIN_S <= idle <= PROACTIVE_IDLE_MAX_S):
            return False
        return self._rng.random() < PROACTIVE_IDLE_PROB

    def _work_context(self) -> str:
        bits: list[str] = []
        try:
            for term in ("project", "building", "working on", "launch", "deadline", "startup"):
                for f in self._semantic.search(term, limit=3):
                    line = f"- {f.key.replace('_', ' ').replace('.', ' › ')}: {f.value}"
                    if line not in bits:
                        bits.append(line)
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(bits[:8]) or "- (nothing specific on record)"

    def _idle_prompt(self) -> str:
        # Format the {user} placeholder on the STATIC header ONLY, then append the work context —
        # so a fact value containing a literal '{' or '}' can never crash str.format.
        header = (
            "[Self-initiate — a quiet lull in your live conversation with {user}. If you have "
            "something genuine and SPECIFIC to say right now — a question about what he's working "
            "on, a dry observation, or a brief check-in — say it in ONE short sentence, by name. "
            "If you have nothing real, reply with exactly <SILENT>. Don't greet, don't offer help "
            "generically.]\nWhat you currently know that might be relevant:\n"
        ).format(user=JARVIS_USER_NAME)
        return header + self._work_context()

    async def _compose_idle(self) -> str | None:
        prompt = self._idle_prompt()
        try:
            from app.services.llm import get_llm
            out = await get_llm().chat(prompt, temperature=0.7)
        except Exception:  # noqa: BLE001
            return None
        out = (out or "").strip().strip('"').strip()
        if not out or "<SILENT>" in out.upper() or len(out) > 240:
            return None
        return out

    # ------------------------------------------------------------------ #
    # Control surface (pause / resume / status) — used by the proactive_control tool
    # ------------------------------------------------------------------ #
    def pause(self, minutes: int) -> int:
        self._store.set_paused_until(time.time() + max(1, minutes) * 60)
        return minutes

    def resume(self) -> None:
        self._store.set_paused_until(0)

    def status(self) -> dict:
        now = time.time()
        return {
            "enabled": PROACTIVE_ENABLED,
            "paused": self._store.paused_until() > now,
            "paused_until": self._store.paused_until(),
            "today": self._store.count_today(now),
            "daily_cap": PROACTIVE_DAILY_CAP,
            "quiet_hours": self._quiet_hours(now),
            "register": self._register(),
            "active_streak_min": int((now - self._active_since) / 60) if self._active_since else 0,
            "recent": self._store.recent(limit=5),
        }


_engine: ProactiveEngine | None = None


def get_engine() -> ProactiveEngine:
    global _engine
    if _engine is None:
        _engine = ProactiveEngine()
    return _engine
