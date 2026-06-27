"""
Feeds tools (Phase 10.B) — live intel by voice.

  whats_happening — a ~30-second spoken briefing from the live feeds (markets, watchlist, headlines,
                    his city's weather/air). NON-terminal: the model delivers the digest naturally.
  market_check    — one-off live price for a coin or ticker ("what's Bitcoin at?", "how's TSLA?").
  watch / unwatch / list_watches — manage what JARVIS monitors in the background for anomaly alerts.

The briefing/market lookups are async (network); since the agent's event loop is already running we
run the coroutine on a fresh thread (same trick the messaging tools use).
"""

from __future__ import annotations

import asyncio
import threading

from app.tools import tool
from app.services import feeds


def _run_coro(coro):
    """Run an async coroutine to completion from this sync tool, off the agent's running loop."""
    box: dict = {}
    def _t() -> None:
        try:
            box["v"] = asyncio.run(coro)
        except Exception as e:  # noqa: BLE001
            box["e"] = e
    th = threading.Thread(target=_t)
    th.start()
    th.join()
    if "e" in box:
        raise box["e"]
    return box.get("v")


@tool(
    "Give him a short, live briefing on what's happening — markets and the coins/stocks he's "
    "watching, his city's weather and air, and the top headlines. Use for 'what's happening', "
    "'brief me', 'what's the world up to', 'any news'. The data comes back live; deliver it as a "
    "natural ~30-second spoken briefing, most important first — never invent a number or headline.",
    narration="Pulling the latest",
    terminal=False,
)
def whats_happening() -> str:
    try:
        return _run_coro(feeds.briefing())
    except Exception:  # noqa: BLE001
        return ("I couldn't reach the feeds just now, sir — say so plainly and offer to try again "
                "in a moment.")


@tool(
    "Get a single live price for a cryptocurrency or a stock ('what's Bitcoin at?', 'how's AAPL "
    "doing?', 'price of Tesla'). Pass the coin name or ticker. Returns the real current price + "
    "change; if the source is down or the symbol's unknown it says so — never guesses a number.",
    params={"query": {"type": "string", "description": "a coin name ('bitcoin') or ticker ('AAPL', 'TSLA')"}},
    required=["query"],
    narration="Checking the market",
    terminal=True,
)
def market_check(query: str) -> str:
    try:
        return _run_coro(feeds.market_check(query))
    except Exception:  # noqa: BLE001
        return f"I couldn't get a live quote for {query}, sir — the source may be down."


@tool(
    "Add something to his watchlist so you monitor it in the background and flag big changes. "
    "kind is one of: crypto (a coin like bitcoin), stock (a ticker like AAPL), github (owner/repo), "
    "reddit (a subreddit), news (a keyword/topic), aqi (a city's air quality), quake (earthquakes "
    "near a city). Use for 'keep an eye on X', 'watch X', 'alert me if X moves'. For a price, an "
    "optional threshold is the percent move that triggers an alert.",
    params={
        "kind": {"type": "string", "description": "crypto | stock | github | reddit | news | aqi | quake"},
        "target": {"type": "string", "description": "the coin/ticker/repo/subreddit/keyword/city"},
        "threshold": {"type": "number", "description": "optional — % move that triggers a price alert"},
    },
    required=["kind", "target"],
    narration="Adding to the watchlist",
    terminal=True,
)
def watch(kind: str, target: str, threshold: float = 0.0) -> str:
    res = feeds.add_watch(kind, target, label=target, threshold=threshold)
    if not res.get("ok"):
        return res.get("message", "I can't watch that, sir.")
    extra = f" — I'll flag a move past {threshold:g}%" if threshold else ""
    return f"Watching {res['kind']} {target} now, sir{extra}. I'll let you know if it moves."


@tool(
    "Remove something from his watchlist. Use for 'stop watching X', 'remove X from my watchlist', "
    "'you can stop tracking X'.",
    params={"target": {"type": "string", "description": "the coin/ticker/repo/keyword/city to drop"}},
    required=["target"],
    narration="Updating the watchlist",
    terminal=True,
)
def unwatch(target: str) -> str:
    n = feeds.remove_watch(target)
    return (f"Done, sir — I've stopped watching {target}." if n else
            f"I wasn't watching {target}, sir.")


@tool(
    "List what JARVIS is currently watching in the background (his watchlist). Use when he asks "
    "'what are you watching', 'what's on my watchlist'.",
    narration="Checking the watchlist",
    terminal=True,
)
def list_watches() -> str:
    ws = feeds.watches()
    if not ws:
        return "Your watchlist is empty, sir — tell me what to keep an eye on."
    by = {}
    for w in ws:
        by.setdefault(w.kind, []).append(w.label or w.target)
    parts = [f"{kind}: {', '.join(items)}" for kind, items in by.items()]
    return "On your watchlist, sir — " + "; ".join(parts) + "."
