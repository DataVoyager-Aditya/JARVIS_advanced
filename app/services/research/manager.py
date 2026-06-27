"""
Phase 10.A — the research manager (background worker).

A deep sweep takes tens of seconds to a couple of minutes, so it must NOT block the conversation.
This manager owns a single dedicated daemon thread running its own asyncio loop; sweeps run there
while JARVIS keeps talking. The flow:

  * submit(topic)   — admission control (dedup the same topic, cap concurrent sweeps), then schedules
                      a sweep on the worker loop and returns IMMEDIATELY. The tool speaks a kickoff
                      line; the listener narrates progress + the final briefing as they land.
  * drain_progress()/drain_done() — the router endpoints the listener polls to speak live updates and
                      the finished briefing. Transient, in-memory, thread-safe (deque + lock).
  * status()        — what's running now + the last finished briefing (backs the research_status tool).
  * run_blocking(topic) — run a sweep on the worker loop and WAIT for the briefing dict (used by the
                      continuous-monitoring scheduler, which needs the result to detect change).

On completion a sweep is saved to research.db, a digest is filed into long-term (episodic) memory,
and a spoken "it's ready" line is queued. Every sweep is wrapped so a failure still cleans up and
surfaces an honest line — it never leaves a topic stuck "in progress".
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque

from config import RESEARCH_MAX_CONCURRENT, MEMORY_ENABLED
from app.services.llm.key_rotator import get_rotator
from app.services.research.engine import Researcher
from app.services.research.store import get_research_store, topic_key

logger = logging.getLogger("jarvis.research.manager")


class ResearchManager:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._progress: deque = deque(maxlen=200)     # spoken progress lines awaiting the listener
        self._done: deque = deque(maxlen=50)          # finished-briefing announcements
        self._active: dict[str, dict] = {}            # topic_key -> {topic, started, progress}
        self._last_done: dict | None = None           # most recent finished briefing (for status)

    # ------------------------------------------------------------------ #
    # worker loop lifecycle
    # ------------------------------------------------------------------ #
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Return the dedicated worker loop, creating it once. Everything happens UNDER the lock so
        two concurrent first-callers (e.g. a user submit racing the monitor) can't spawn two worker
        threads. The loop object is created synchronously here, then handed to the thread to run —
        run_coroutine_threadsafe is safe to call the instant the object exists (it queues + wakes the
        loop once run_forever starts)."""
        with self._lock:
            if (self._loop is not None and not self._loop.is_closed()
                    and self._thread is not None and self._thread.is_alive()):
                return self._loop
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._thread = threading.Thread(target=loop.run_forever, name="research-worker", daemon=True)
            self._thread.start()
            return loop

    # ------------------------------------------------------------------ #
    # progress / done buffers (thread-safe)
    # ------------------------------------------------------------------ #
    def _push_progress(self, topic_k: str, line: str) -> None:
        line = (line or "").strip()
        if not line:
            return
        with self._lock:
            self._progress.append(line)
            if topic_k in self._active:
                self._active[topic_k]["progress"] = line

    def drain_progress(self) -> list[str]:
        with self._lock:
            out = list(self._progress)
            self._progress.clear()
        return out

    def drain_done(self) -> list[dict]:
        with self._lock:
            out = list(self._done)
            self._done.clear()
        return out

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def submit(self, topic: str) -> dict:
        topic = (topic or "").strip()
        if not topic:
            return {"ok": False, "status": "empty", "message": "no topic"}
        key = topic_key(topic)
        try:
            loop = self._ensure_loop()
        except Exception as e:  # noqa: BLE001
            logger.exception("research worker unavailable")
            return {"ok": False, "status": "error", "message": str(e)}
        with self._lock:
            self._reap_stale()
            if key in self._active:
                return {"ok": True, "status": "already_running", "topic": topic}
            if len(self._active) >= RESEARCH_MAX_CONCURRENT:
                running = ", ".join(a["topic"] for a in self._active.values())
                return {"ok": False, "status": "busy", "topic": topic,
                        "message": f"already running: {running}"}
            self._active[key] = {"topic": topic, "started": time.time(), "progress": ""}
        asyncio.run_coroutine_threadsafe(self._run(topic, key, announce=True), loop)
        logger.info("research submitted: %r", topic[:80])
        return {"ok": True, "status": "started", "topic": topic}

    def run_blocking(self, topic: str, timeout: float = 600.0) -> dict | None:
        """Run a sweep on the worker loop and WAIT for the briefing dict (no announce) — for the
        monitoring scheduler. Marks the topic ACTIVE for the duration so a concurrent user submit
        dedups against it (and vice-versa). Returns None on timeout/error."""
        key = topic_key(topic)
        try:
            loop = self._ensure_loop()
        except Exception as e:  # noqa: BLE001
            logger.warning("run_blocking(%r): worker unavailable: %s", topic[:50], e)
            return None
        with self._lock:
            owns = key not in self._active        # don't clobber a user sweep already running it
            if owns:
                self._active[key] = {"topic": topic, "started": time.time(),
                                     "progress": "(monitor re-run)"}
        try:
            fut = asyncio.run_coroutine_threadsafe(self._sweep(topic), loop)
            return fut.result(timeout=timeout)
        except Exception as e:  # noqa: BLE001
            logger.warning("run_blocking(%r) failed: %s", topic[:50], e)
            return None
        finally:
            if owns:
                with self._lock:
                    self._active.pop(key, None)

    def status(self, topic: str | None = None) -> dict:
        with self._lock:
            active = [{"topic": a["topic"], "elapsed_s": int(time.time() - a["started"]),
                       "progress": a["progress"]} for a in self._active.values()]
            last = dict(self._last_done) if self._last_done else None
        return {"active": active, "busy": bool(active), "last": last}

    def is_active(self, topic: str) -> bool:
        with self._lock:
            return topic_key(topic) in self._active

    def _reap_stale(self, max_age_s: float = 900.0) -> None:
        """Drop _active entries older than max_age (a sweep can't legitimately run 15 min — the time
        budget + LLM HTTP timeouts cap it well under that). Self-heals an orphan left by a worker-loop
        death so a stuck topic can't permanently consume a concurrency slot. Call under the lock."""
        now = time.time()
        stale = [k for k, a in self._active.items() if now - a["started"] > max_age_s]
        for k in stale:
            logger.warning("reaping orphaned active sweep %r", self._active[k]["topic"][:50])
            self._active.pop(k, None)

    def announce_update(self, briefing: dict, label: str) -> None:
        """Queue a 'there's been a development' line for a continuously-monitored topic whose re-run
        turned up a material change (used by the monitor scheduler)."""
        summary = (briefing.get("summary") or "").strip()
        line = (f"A development on {label}, sir. {summary} I've refreshed the briefing."
                if summary else f"There's movement on {label}, sir — I've refreshed the briefing.")
        item = {"topic": label, "summary": summary, "speak": line, "ok": True,
                "kind": "update", "ts": time.time()}
        with self._lock:
            self._done.append(item)
            self._last_done = {"topic": label, "ok": True, "ts": item["ts"]}

    # ------------------------------------------------------------------ #
    # the sweep
    # ------------------------------------------------------------------ #
    async def _sweep(self, topic: str) -> dict:
        """Run one Researcher pass and return the briefing dict. Progress lines route to the buffer."""
        key = topic_key(topic)
        researcher = Researcher(get_rotator(), on_progress=lambda ln: self._push_progress(key, ln))
        return await researcher.run(topic)

    async def _run(self, topic: str, key: str, announce: bool) -> None:
        briefing: dict | None = None
        try:
            briefing = await self._sweep(topic)
            await asyncio.to_thread(self._persist, briefing)
        except Exception:  # noqa: BLE001
            logger.exception("research sweep crashed: %r", topic[:60])
            briefing = {"topic": topic, "title": topic, "ok": False, "n_sources": 0,
                        "summary": f"I hit a snag finishing that sweep on {topic}, sir — give me "
                                   "another go at it in a moment.",
                        "full_md": "", "confidence": "None", "signature": "", "sources": []}
        finally:
            with self._lock:
                self._active.pop(key, None)
        if announce and briefing is not None:
            self._announce(briefing)

    def _announce(self, briefing: dict) -> None:
        """Queue the finished-briefing line the listener speaks, framed in JARVIS's register."""
        topic = briefing.get("title") or briefing.get("topic") or "that"
        summary = (briefing.get("summary") or "").strip()
        if briefing.get("ok") and summary:
            n = briefing.get("n_sources", 0)
            tail = f" I pulled that from {n} sources — the full breakdown's ready when you want it." \
                if n else " The full breakdown's ready when you want it."
            line = f"Finished that deep dive on {topic}, sir. {summary}{tail}"
        else:
            # _empty()/crash summaries are already honest and in-character — speak as-is.
            line = summary or f"I couldn't complete the sweep on {topic}, sir."
        item = {"topic": topic, "summary": summary, "speak": line,
                "ok": bool(briefing.get("ok")), "ts": time.time()}
        with self._lock:
            self._done.append(item)
            self._last_done = {"topic": topic, "ok": item["ok"], "ts": item["ts"]}

    def _persist(self, briefing: dict) -> None:
        """Save the briefing (research.db) and file a recallable digest into episodic memory. Both are
        best-effort — a storage hiccup must not lose the spoken result."""
        if not briefing or not briefing.get("ok"):
            return
        try:
            get_research_store().save_briefing(
                topic=briefing["topic"], title=briefing.get("title", briefing["topic"]),
                summary=briefing.get("summary", ""), full_md=briefing.get("full_md", ""),
                sources=briefing.get("sources", []), confidence=briefing.get("confidence", ""),
                signature=briefing.get("signature", ""))
        except Exception:  # noqa: BLE001
            logger.exception("save_briefing failed")
        if MEMORY_ENABLED and briefing.get("summary"):
            try:
                from app.services.memory import get_memory
                # key=None -> episodic only (recallable), NOT injected into every prompt (avoids bloat).
                get_memory().remember_fact(
                    f"Deep-research briefing on {briefing['topic']}: {briefing['summary']}")
            except Exception:  # noqa: BLE001
                logger.debug("memory persist of briefing skipped", exc_info=True)


_manager: ResearchManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> ResearchManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = ResearchManager()
    return _manager
