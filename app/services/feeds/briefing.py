"""
Phase 10.B — on-demand spoken briefing + one-off market lookups.

`briefing()` assembles the live state of the things he cares about (his watchlist markets, his
city's weather/air, top headlines, any fresh alerts) into a compact digest the model then speaks as a
natural ~30-second briefing. `market_check()` answers a single "what's Bitcoin / AAPL at?" question.
Everything is real and live; a dead source is simply skipped, never faked.
"""

from __future__ import annotations

import logging
import sqlite3

import httpx

from config import MEMORY_DB
from app.services.feeds import sources
from app.services.feeds.store import get_feeds_store

logger = logging.getLogger("jarvis.feeds.briefing")

# Common crypto aliases -> CoinGecko id (so "btc"/"bitcoin" both work without a lookup call).
_CRYPTO = {
    "btc": "bitcoin", "bitcoin": "bitcoin", "eth": "ethereum", "ethereum": "ethereum",
    "sol": "solana", "solana": "solana", "bnb": "binancecoin", "xrp": "ripple", "ripple": "ripple",
    "ada": "cardano", "cardano": "cardano", "doge": "dogecoin", "dogecoin": "dogecoin",
    "matic": "matic-network", "polygon": "matic-network", "dot": "polkadot", "polkadot": "polkadot",
}


def _user_city() -> str:
    try:
        if MEMORY_DB.exists():
            con = sqlite3.connect(str(MEMORY_DB))
            con.execute("PRAGMA busy_timeout=5000")     # wait out a concurrent memory write instead of
            row = con.execute(                          # failing instantly into the wrong-city fallback
                "SELECT value FROM facts WHERE key='user.location'").fetchone()
            con.close()
            if row and row[0]:
                return row[0].split(",")[0].strip()
    except Exception:  # noqa: BLE001
        pass
    return "New Delhi"


async def briefing() -> str:
    """A digest string for the model to deliver as a natural spoken briefing (most important first)."""
    store = get_feeds_store()
    watches = store.watches()
    bits: list[str] = []
    async with httpx.AsyncClient(headers=sources.UA) as client:
        crypto_ids = [w.target for w in watches if w.kind == "crypto"] or ["bitcoin", "ethereum"]
        prices = await sources.crypto_prices(client, crypto_ids)
        for cid in crypto_ids:
            p = prices.get(cid)
            if p:
                bits.append(f"{cid.replace('-', ' ').title()} ${p['price']:,.0f} ({p['chg24h']:+.1f}% 24h)")
        for w in [w for w in watches if w.kind == "stock"]:
            q = await sources.stock_quote(client, w.target)
            if q:
                bits.append(f"{w.label} ${q['price']:,.2f} ({q['chg_pct']:+.1f}% since open)")
        city = _user_city()
        wx = await sources.weather(client, city)
        if wx and wx.get("temp") is not None:
            wline = f"{wx['city']} {round(wx['temp'])}°C"
            loc = await sources.geocode(client, city)
            if loc:
                aq = await sources.air_quality(client, loc["lat"], loc["lon"])
                if aq:
                    wline += f", air quality index {aq['aqi']}"
            bits.append(wline)
        heads = [h["title"] for h in await sources.hn_front(client, 3) if h.get("title")]
        for w in [w for w in watches if w.kind in ("news", "reddit")][:2]:
            items = (await sources.news_search(client, w.target, 2) if w.kind == "news"
                     else await sources.reddit_new(client, w.target, 2))
            heads += [it["title"] for it in (items or [])[:1] if it.get("title")]

    alerts = [a["line"] for a in store.recent_alerts(3)]
    parts = ["Live readings — relay these as a natural ~30-second spoken briefing, most important "
             "first, conversational, NOT a list read-out. Don't invent anything; skip what's missing."]
    if bits:
        parts.append("Markets/weather: " + " | ".join(bits))
    if heads:
        parts.append("Headlines: " + " ; ".join(h[:90] for h in heads[:4]))
    if alerts:
        parts.append("Recent alerts: " + " ; ".join(alerts[:2]))
    if len(parts) == 1:
        return "No live feeds are reachable right now, sir — say so plainly and offer to try again."
    return "\n".join(parts)


async def market_check(query: str) -> str:
    """A single live price for a coin or ticker ('bitcoin', 'AAPL', 'TSLA'). Real or honest-miss."""
    q = (query or "").strip()
    if not q:
        return "Which market would you like, sir — a coin or a ticker?"
    low = q.lower()
    async with httpx.AsyncClient(headers=sources.UA) as client:
        if low in _CRYPTO:
            cid = _CRYPTO[low]
            p = (await sources.crypto_prices(client, [cid])).get(cid)
            if p:
                return (f"{cid.title()} is at ${p['price']:,.2f}, {p['chg24h']:+.1f}% over 24 hours, sir.")
        # try a stock ticker
        s = await sources.stock_quote(client, q)
        if s:
            return f"{s['symbol']} is at ${s['price']:,.2f}, {s['chg_pct']:+.1f}% since the open, sir."
        # last resort: maybe it's a coin id we don't have aliased
        p = (await sources.crypto_prices(client, [low])).get(low)
        if p:
            return f"{low.title()} is at ${p['price']:,.2f}, {p['chg24h']:+.1f}% over 24 hours, sir."
    return f"I couldn't get a live quote for {q}, sir — the source may be down or the symbol unknown."
