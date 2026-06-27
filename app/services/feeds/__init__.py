"""
Phase 10.B — real-time intelligence feeds (facade).

JARVIS watches the boss's watchlist in the background and flags significant changes, and gives a
spoken briefing on demand. All free/no-key. The desktop listener drains spoken alerts via
`monitor.drain()`; the backend startup calls `start_monitor()`.
"""

from __future__ import annotations

from app.services.feeds.store import FeedsStore, Watch, get_feeds_store
from app.services.feeds.monitor import check_all, drain, start_monitor, monitor_loop
from app.services.feeds.briefing import briefing, market_check

# Watch kinds JARVIS understands, with a human label for the tool/persona.
KINDS = {
    "crypto": "a cryptocurrency (CoinGecko id, e.g. bitcoin)",
    "stock": "a stock ticker (e.g. AAPL, or tcs.in)",
    "github": "a GitHub repo (owner/repo)",
    "reddit": "a subreddit",
    "news": "a news keyword/topic",
    "aqi": "a city's air quality",
    "quake": "earthquakes near a city",
}


def add_watch(kind: str, target: str, label: str = "", threshold: float = 0.0) -> dict:
    kind = (kind or "").lower().strip()
    target = (target or "").strip()
    if kind not in KINDS or not target:
        return {"ok": False, "message": "I can watch a coin, stock, GitHub repo, subreddit, news "
                                        "keyword, a city's air, or earthquakes near a city, sir."}
    wid = get_feeds_store().add_watch(kind, target, label or target, threshold)
    return {"ok": True, "id": wid, "kind": kind, "target": target}


def remove_watch(target: str, kind: str | None = None) -> int:
    return get_feeds_store().remove_watch(target, kind)


def watches(kind: str | None = None) -> list[Watch]:
    return get_feeds_store().watches(kind)


__all__ = ["FeedsStore", "Watch", "get_feeds_store", "check_all", "drain", "start_monitor",
           "monitor_loop", "briefing", "market_check", "KINDS", "add_watch", "remove_watch", "watches"]
