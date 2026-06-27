"""
Phase 10.A — continuous topic monitoring ("keep watching <topic>").

A light scheduler on the backend loop. Every RESEARCH_MONITOR_TICK_S it asks the store which
monitored topics are DUE (last run older than their interval), re-runs the sweep on the worker
loop, and:
  * first run for a topic   -> establish the baseline signature, save the briefing, NO alert
                               (seed-then-alert — never a false "it changed!" on the very first pass).
  * material change          -> the new key-findings fingerprint differs from last -> save the fresh
                               briefing and queue a spoken "there's been a development on X" line.
  * no change                -> just bump last_run_ts (don't store a duplicate briefing).

During quiet hours (shared with the proactive window) re-runs are HELD so a development isn't spoken
at 3am; they fire on the next tick after quiet hours. A re-run is skipped if the user is already
sweeping that exact topic right now (no double work).
"""

from __future__ import annotations

import asyncio
import logging
import time

from config import (
    RESEARCH_MONITOR_TICK_S, RESEARCH_QUIET_ALERTS,
    PROACTIVE_QUIET_START, PROACTIVE_QUIET_END,
)
from app.services.research.store import get_research_store
from app.services.research.manager import get_manager

logger = logging.getLogger("jarvis.research.monitor")

_MAX_PER_TICK = 3   # at most N due topics re-run per tick, so one busy tick can't stack up sweeps


def _quiet_now(now: float | None = None) -> bool:
    h = time.localtime(now if now is not None else time.time()).tm_hour
    s, e = PROACTIVE_QUIET_START, PROACTIVE_QUIET_END
    return (s <= h or h < e) if s > e else (s <= h < e)   # handles the midnight wrap (e.g. 23->8)


class ResearchMonitor:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def _process_due(self) -> None:
        if RESEARCH_QUIET_ALERTS and _quiet_now():
            return                                   # hold re-runs until quiet hours pass
        store = get_research_store()
        mgr = get_manager()
        due = store.due_monitors()[:_MAX_PER_TICK]   # bound the work one tick can take on
        for m in due:
            if mgr.is_active(m.topic):               # user (or another tick) already sweeping it
                continue
            briefing = await asyncio.to_thread(mgr.run_blocking, m.topic)
            if not briefing or not briefing.get("ok"):
                # Don't burn the baseline on a transient failure; leave last_run so it retries next tick.
                logger.info("monitor re-run yielded nothing for %r — will retry", m.topic[:50])
                continue
            sig = briefing.get("signature", "")
            # Store writes go off the backend event loop (they take store._lock + fsync) so the
            # monitor tick never blocks request/conversation serving.
            if not m.last_signature:                 # baseline — seed, no alert
                await asyncio.to_thread(self._save, store, briefing)
                await asyncio.to_thread(store.mark_monitor_run, m.topic, sig)
                logger.info("monitor baseline established for %r", m.topic[:50])
            elif sig and sig != m.last_signature:    # material change
                await asyncio.to_thread(self._save, store, briefing)
                await asyncio.to_thread(store.mark_monitor_run, m.topic, sig)
                mgr.announce_update(briefing, m.label)
                logger.info("monitor detected development on %r", m.topic[:50])
            else:                                    # unchanged — just bump the clock
                await asyncio.to_thread(store.mark_monitor_run, m.topic, m.last_signature or sig)

    @staticmethod
    def _save(store, briefing: dict) -> None:
        try:
            store.save_briefing(
                topic=briefing["topic"], title=briefing.get("title", briefing["topic"]),
                summary=briefing.get("summary", ""), full_md=briefing.get("full_md", ""),
                sources=briefing.get("sources", []), confidence=briefing.get("confidence", ""),
                signature=briefing.get("signature", ""))
        except Exception:  # noqa: BLE001
            logger.exception("monitor save_briefing failed")

    async def _loop(self) -> None:
        # Small initial delay so it doesn't fire during startup warm-up.
        await asyncio.sleep(min(60, RESEARCH_MONITOR_TICK_S))
        while True:
            try:
                await self._process_due()
            except Exception:  # noqa: BLE001 — one bad tick must never kill the scheduler
                logger.exception("research monitor tick failed")
            await asyncio.sleep(RESEARCH_MONITOR_TICK_S)

    def start(self) -> None:
        if self._task is not None:
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self._loop())
            logger.info("research topic-monitor started (tick %ds)", RESEARCH_MONITOR_TICK_S)
        except RuntimeError:
            logger.debug("start_monitor called outside an event loop")


_monitor: ResearchMonitor | None = None


def get_monitor() -> ResearchMonitor:
    global _monitor
    if _monitor is None:
        _monitor = ResearchMonitor()
    return _monitor


def start_monitor() -> None:
    get_monitor().start()
