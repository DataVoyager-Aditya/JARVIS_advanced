"""
Phase 10.B — free, no-key data sources for JARVIS's intel feeds.

Every function here is non-raising: on any network/parse error it returns an empty result (None /
[] / {}), so one dead source never breaks a briefing or the monitor. The caller passes a shared
httpx.AsyncClient so connections are reused. No source needs an API key or a credit card:

  crypto  — CoinGecko simple/price            stocks  — Stooq last-quote CSV
  weather — open-meteo forecast               air     — open-meteo air-quality (US AQI)
  quakes  — USGS GeoJSON feed                 github  — api.github.com repo (optional token)
  reddit  — reddit.com/r/<sub>/new.json       hn      — hn.algolia.com front_page
  news    — Google News RSS search            rss     — any RSS/Atom title list
"""

from __future__ import annotations

import html
import logging
import math
import re
from urllib.parse import quote

import httpx

from config import GITHUB_TOKEN

logger = logging.getLogger("jarvis.feeds.sources")

UA = {"User-Agent": "JARVIS/1.0 (personal assistant)"}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in km."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


async def crypto_prices(client: httpx.AsyncClient, ids: list[str]) -> dict[str, dict]:
    """CoinGecko ids (e.g. 'bitcoin','ethereum') -> {id: {'price': float, 'chg24h': float}}."""
    if not ids:
        return {}
    try:
        r = await client.get("https://api.coingecko.com/api/v3/simple/price",
                             params={"ids": ",".join(ids), "vs_currencies": "usd",
                                     "include_24hr_change": "true"}, timeout=8)
        d = r.json()
        out: dict[str, dict] = {}
        for cid in ids:
            if cid in d and "usd" in d[cid]:
                out[cid] = {"price": float(d[cid]["usd"]),
                            "chg24h": float(d[cid].get("usd_24h_change", 0.0) or 0.0)}
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("crypto fetch failed: %s", e)
        return {}


async def stock_quote(client: httpx.AsyncClient, symbol: str) -> dict | None:
    """Stooq last quote (free CSV). Returns {'price','chg_pct','symbol'} or None. A bare ticker is
    assumed US ('AAPL' -> 'aapl.us'); pass an explicit suffix for other markets ('tcs.in')."""
    s = (symbol or "").strip().lower()
    if not s:
        return None
    if "." not in s:
        s += ".us"
    try:
        r = await client.get("https://stooq.com/q/l/", timeout=8,
                             params={"s": s, "f": "sd2t2ohlcv", "h": "", "e": "csv"})
        lines = r.text.strip().splitlines()
        if len(lines) < 2:
            return None
        cols = lines[1].split(",")
        if len(cols) < 7 or "N/D" in lines[1]:
            return None
        o, c = float(cols[3]), float(cols[6])
        chg = ((c - o) / o * 100.0) if o else 0.0
        return {"symbol": symbol.upper(), "price": c, "chg_pct": chg}
    except Exception as e:  # noqa: BLE001
        logger.debug("stock fetch failed (%s): %s", symbol, e)
        return None


async def geocode(client: httpx.AsyncClient, city: str) -> dict | None:
    try:
        g = (await client.get("https://geocoding-api.open-meteo.com/v1/search",
                              params={"name": city, "count": 1}, timeout=8)).json()
        if not g.get("results"):
            return None
        loc = g["results"][0]
        return {"name": loc["name"], "lat": float(loc["latitude"]), "lon": float(loc["longitude"])}
    except Exception as e:  # noqa: BLE001
        logger.debug("geocode failed (%s): %s", city, e)
        return None


async def weather(client: httpx.AsyncClient, city: str) -> dict | None:
    loc = await geocode(client, city)
    if not loc:
        return None
    try:
        w = (await client.get("https://api.open-meteo.com/v1/forecast", timeout=8, params={
            "latitude": loc["lat"], "longitude": loc["lon"],
            "current": "temperature_2m,relative_humidity_2m,weather_code"})).json()
        cur = w.get("current", {})
        return {"city": loc["name"], "temp": cur.get("temperature_2m"),
                "humidity": cur.get("relative_humidity_2m"), "code": cur.get("weather_code")}
    except Exception as e:  # noqa: BLE001
        logger.debug("weather failed (%s): %s", city, e)
        return None


async def air_quality(client: httpx.AsyncClient, lat: float, lon: float) -> dict | None:
    try:
        a = (await client.get("https://air-quality-api.open-meteo.com/v1/air-quality", timeout=8,
                              params={"latitude": lat, "longitude": lon, "current": "us_aqi"})).json()
        aqi = a.get("current", {}).get("us_aqi")
        return {"aqi": int(aqi)} if aqi is not None else None
    except Exception as e:  # noqa: BLE001
        logger.debug("air quality failed: %s", e)
        return None


async def earthquakes(client: httpx.AsyncClient, min_mag: float = 4.5) -> list[dict] | None:
    """Recent quakes (last day) at or above min_mag from the USGS feed. Returns None on a FETCH
    failure (vs [] for 'fetched fine, no quakes') so the monitor never mistakes a network blip for an
    empty baseline. One malformed feature can't drop the rest (per-feature guard)."""
    try:
        feed = {2.5: "2.5_day", 4.5: "4.5_day"}.get(min_mag, "2.5_day")
        url = f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{feed}.geojson"
        r = await client.get(url, timeout=8)
        if r.status_code != 200:
            return None
        d = r.json()
        out = []
        for f in d.get("features", []):
            try:
                props = f.get("properties") or {}
                geom = f.get("geometry") or {}          # 'geometry': null -> {} (not None)
                mag = props.get("mag")
                coords = geom.get("coordinates") or [None, None]
                if (mag is None or mag < min_mag or len(coords) < 2
                        or coords[0] is None or coords[1] is None):
                    continue
                out.append({"mag": float(mag), "place": props.get("place", "unknown"),
                            "lon": float(coords[0]), "lat": float(coords[1]),
                            "ts": float(props.get("time", 0) or 0) / 1000.0, "id": f.get("id", "")})
            except Exception:  # noqa: BLE001 — one bad feature must not drop the whole feed
                continue
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("quakes failed: %s", e)
        return None


async def github_repo(client: httpx.AsyncClient, owner_repo: str) -> dict | None:
    """{'name','stars','issues'} for 'owner/repo'. Uses the optional GITHUB_TOKEN only to raise the
    free rate limit (60/hr unauth is plenty for a handful of repos)."""
    s = (owner_repo or "").strip().strip("/")
    if "/" not in s:
        return None
    headers = dict(UA)
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    try:
        r = await client.get(f"https://api.github.com/repos/{s}", headers=headers, timeout=8)
        if r.status_code != 200:
            return None
        d = r.json()
        return {"name": d.get("full_name", s), "stars": int(d.get("stargazers_count", 0)),
                "issues": int(d.get("open_issues_count", 0))}
    except Exception as e:  # noqa: BLE001
        logger.debug("github failed (%s): %s", owner_repo, e)
        return None


async def reddit_new(client: httpx.AsyncClient, sub: str, limit: int = 5) -> list[dict] | None:
    """Newest posts in a subreddit. Returns None on a FETCH failure (so the monitor doesn't seed an
    empty baseline off a 429), [] only on a genuine empty success."""
    s = (sub or "").strip()
    if s[:2].lower() == "r/":           # strip the 'r/' PREFIX (not arbitrary leading r's/slashes)
        s = s[2:]
    s = s.strip("/").strip()
    if not s:
        return None
    try:
        r = await client.get(f"https://www.reddit.com/r/{s}/new.json",
                             headers=UA, params={"limit": limit}, timeout=8)
        if r.status_code != 200:        # reddit's public JSON rate-limits with 429 often
            return None
        d = r.json()
        out = []
        for child in d.get("data", {}).get("children", []):
            p = child.get("data", {})
            if p.get("title"):
                out.append({"title": p["title"], "url": "https://reddit.com" + p.get("permalink", ""),
                            "ts": float(p.get("created_utc", 0)), "id": p.get("id", "")})
        return out[:limit]
    except Exception as e:  # noqa: BLE001
        logger.debug("reddit failed (%s): %s", sub, e)
        return None


async def hn_front(client: httpx.AsyncClient, limit: int = 5) -> list[dict]:
    try:
        hits = (await client.get("https://hn.algolia.com/api/v1/search",
                                params={"tags": "front_page"}, timeout=8)).json().get("hits", [])
        return [{"title": h.get("title", ""), "url": h.get("url", ""), "id": str(h.get("objectID", ""))}
                for h in hits[:limit] if h.get("title")]
    except Exception as e:  # noqa: BLE001
        logger.debug("hn failed: %s", e)
        return []


def _rss_titles(text: str, limit: int) -> list[dict]:
    items = re.findall(r"<item>(.*?)</item>", text, re.DOTALL | re.IGNORECASE)
    if not items:
        items = re.findall(r"<entry>(.*?)</entry>", text, re.DOTALL | re.IGNORECASE)  # Atom
    out = []
    for it in items[:limit]:
        # `<title ...>` may carry attributes (Atom: type="html"/"text") — allow them.
        m = re.search(r"<title\b[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                      it, re.DOTALL | re.IGNORECASE)
        if m and m.group(1).strip():
            # strip nested tags THEN decode HTML entities so a headline reads "AT&T", not "AT&amp;T".
            title = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
            if title:
                out.append({"title": title})
    return out


async def rss_titles(client: httpx.AsyncClient, url: str, limit: int = 3) -> list[dict] | None:
    """Returns None on a FETCH failure (vs [] for 'fetched, no items')."""
    try:
        r = await client.get(url, headers=UA, timeout=8)
        if r.status_code != 200:
            return None
        return _rss_titles(r.text, limit)
    except Exception as e:  # noqa: BLE001
        logger.debug("rss failed (%s): %s", url, e)
        return None


async def news_search(client: httpx.AsyncClient, query: str, limit: int = 4) -> list[dict]:
    """Headlines matching a keyword via Google News RSS (free, no key)."""
    q = (query or "").strip()
    if not q:
        return []
    url = f"https://news.google.com/rss/search?q={quote(q)}&hl=en-US&gl=US&ceid=US:en"
    return await rss_titles(client, url, limit)
