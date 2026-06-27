"""
Phase 10.A — Autonomous deep research (the single facade the rest of JARVIS talks to).

  start_research(topic)        kick off a background sweep (returns immediately)
  research_status(topic?)      what's running now + the last finished briefing
  drain_progress()/drain_done()  spoken progress lines / finished-briefing announcements (listener)
  get_briefing(topic)/latest_briefing()/list_briefings()   read back saved briefings
  watch_topic/unwatch_topic/topics   continuous topic monitoring
  start_monitor()              start the daily re-run scheduler (called from app.main startup)

Tools (app/tools/research.py) and the router (app/routers/research.py) only ever import from here.
"""

from __future__ import annotations

from config import RESEARCH_MONITOR_EVERY_H
from app.services.research import fetch as _fetch
from app.services.research.manager import get_manager
from app.services.research.store import get_research_store, topic_key, Briefing, Monitor
from app.services.research.monitor import start_monitor


def search_available() -> bool:
    """Whether a deep sweep can run at all (a Tavily search key is configured)."""
    return _fetch.has_search()


# ---- sweeps --------------------------------------------------------------- #
def start_research(topic: str) -> dict:
    return get_manager().submit(topic)


def research_status(topic: str | None = None) -> dict:
    return get_manager().status(topic)


def drain_progress() -> list[str]:
    return get_manager().drain_progress()


def drain_done() -> list[dict]:
    return get_manager().drain_done()


# ---- briefings ------------------------------------------------------------ #
def get_briefing(topic: str) -> Briefing | None:
    return get_research_store().get_briefing(topic)


def latest_briefing() -> Briefing | None:
    return get_research_store().latest_briefing()


def list_briefings(limit: int = 12) -> list[Briefing]:
    return get_research_store().list_briefings(limit)


# ---- continuous monitoring ------------------------------------------------ #
def watch_topic(topic: str, interval_h: float | None = None) -> dict:
    topic = (topic or "").strip()
    if not topic:
        return {"ok": False, "message": "no topic"}
    get_research_store().add_monitor(
        topic, label=topic, interval_h=interval_h or RESEARCH_MONITOR_EVERY_H)
    return {"ok": True, "topic": topic, "interval_h": interval_h or RESEARCH_MONITOR_EVERY_H}


def unwatch_topic(topic: str) -> int:
    return get_research_store().remove_monitor(topic)


def topics() -> list[Monitor]:
    return get_research_store().monitors()


__all__ = [
    "search_available",
    "start_research", "research_status", "drain_progress", "drain_done",
    "get_briefing", "latest_briefing", "list_briefings",
    "watch_topic", "unwatch_topic", "topics", "start_monitor",
    "get_research_store", "get_manager", "topic_key", "Briefing", "Monitor",
]
