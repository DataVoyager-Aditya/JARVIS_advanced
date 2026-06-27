"""
Phase 10.B — the background intel monitor + anomaly detection.

Every FEEDS_POLL_S the monitor fetches each watched thing, compares it to the last snapshot, and
when something MOVES enough it raises an alert — spoken (buffered for the listener, exactly like the
messaging/calls announce buffers) and shown on the HUD. Detection per kind:

  crypto/stock : a price move >= the watch's threshold within a rolling window ("BTC down 8% in 20m")
  github       : stars gained >= FEEDS_STAR_JUMP since the baseline
  reddit/news  : a NEW headline/post not seen before (first run only seeds 'seen', never spams)
  aqi          : US AQI crossing into 'unhealthy' (>= FEEDS_AQI_BAD)
  quake        : a NEW USGS quake >= FEEDS_QUAKE_MAG within FEEDS_QUAKE_KM of a watched city (CRITICAL)

Guards so it's never noisy: a per-alert cooldown (don't repeat within FEEDS_ALERT_COOLDOWN_S), and —
for everything except a CRITICAL quake-near-people alert — quiet-hours suppression (the alert is still
logged + shown on the HUD, just not spoken at 3am). Snapshots persist, so a restart doesn't re-alert
on a baseline it forgot, and doesn't lose what it has already seen.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from config import (
    FEEDS_ENABLED, FEEDS_POLL_S, FEEDS_MOVE_PCT, FEEDS_MOVE_WINDOW_S, FEEDS_STAR_JUMP,
    FEEDS_QUAKE_MAG, FEEDS_QUAKE_KM, FEEDS_AQI_BAD, FEEDS_ALERT_COOLDOWN_S, FEEDS_QUIET_ALERTS,
    PROACTIVE_QUIET_START, PROACTIVE_QUIET_END,
)
from app.services.feeds import sources
from app.services.feeds.store import get_feeds_store, FeedsStore

logger = logging.getLogger("jarvis.feeds.monitor")

# Transient spoken buffer — drained by the desktop listener (GET /feeds/alerts). Not persisted: on
# restart you don't want yesterday's alerts read out. The alert LOG (for dedup/dashboard) is in the DB.
_buffer: list[dict] = []
_block = asyncio.Lock()
_MAX = 30


def _quiet_now() -> bool:
    h = time.localtime().tm_hour
    s, e = PROACTIVE_QUIET_START, PROACTIVE_QUIET_END
    if s == e:
        return False
    return (s <= h < e) if s < e else (h >= s or h < e)


def drain() -> list[str]:
    """Return + clear pending spoken alert lines (the listener's poll calls this)."""
    lines = [it["line"] for it in _buffer]
    _buffer.clear()
    return lines


async def _broadcast(line: str, kind: str, critical: bool) -> None:
    try:
        from app.routers.events import broadcast
        await broadcast({"type": "notify", "channel": "feeds", "sender": "Intel",
                         "text": line, "line": line, "critical": critical})
    except Exception as e:  # noqa: BLE001
        logger.debug("feeds HUD broadcast failed: %s", e)


async def _fire(store: FeedsStore, dedup_key: str, line: str, *, kind: str, critical: bool = False) -> bool:
    """Raise an alert if it isn't on cooldown. Returns True if it was SPOKEN (buffered). Always logs
    + shows on the HUD; only speaking is held back in quiet hours (unless critical)."""
    if store.alert_recently(dedup_key, FEEDS_ALERT_COOLDOWN_S):
        return False
    store.record_alert(dedup_key, line)
    await _broadcast(line, kind, critical)
    speak = critical or not (FEEDS_QUIET_ALERTS and _quiet_now())
    if speak:
        async with _block:
            _buffer.append({"line": line, "ts": time.time(), "critical": critical})
            if len(_buffer) > _MAX:
                del _buffer[:-_MAX]
    logger.info("feeds alert%s: %s", " (spoken)" if speak else " (held)", line)
    return speak


# ------------------------------------------------------------------ #
# Per-kind checks (each is side-effecting: may _fire + update the snapshot)
# ------------------------------------------------------------------ #
async def _check_price(store: FeedsStore, key: str, label: str, price: float,
                       threshold: float, chg24h: float | None = None) -> None:
    """True ROLLING window: keep the samples within the trailing FEEDS_MOVE_WINDOW_S and compare the
    current price to the OLDEST one still in the window — so a move that builds across what used to be
    a window boundary still aggregates (a tumbling reset would miss it)."""
    now = time.time()
    snap = store.get_snapshot(key)
    hist = snap.get("hist") if isinstance(snap, dict) else None
    if not isinstance(hist, list):
        hist = []
    hist = [h for h in hist if isinstance(h, (list, tuple)) and len(h) == 2
            and (now - h[0]) <= FEEDS_MOVE_WINDOW_S]      # drop samples that fell out of the window
    hist.append([now, price])
    hist = hist[-60:]                                     # bound growth
    base_ts, base = hist[0][0], hist[0][1]
    pct = ((price - base) / base * 100.0) if base else 0.0
    if len(hist) >= 2 and abs(pct) >= threshold:
        mins = max(1, int((now - base_ts) / 60))
        direction = "up" if pct > 0 else "down"
        line = (f"Heads up, sir — {label} is {direction} {abs(pct):.1f}% in the last "
                f"{mins} minute{'s' if mins != 1 else ''}, now ${price:,.2f}.")
        await _fire(store, f"move:{key}", line, kind="market")
        hist = [[now, price]]                             # reset the window after alerting
    store.set_snapshot(key, {"hist": hist})


async def _check_github(store: FeedsStore, repo: dict, label: str) -> None:
    key = f"github:{repo['name']}"
    stars = repo["stars"]
    snap = store.get_snapshot(key)
    base = snap.get("stars") if isinstance(snap, dict) else None
    if base is None:
        store.set_snapshot(key, {"stars": stars, "ts": time.time()})
        return
    gained = stars - base
    if gained >= FEEDS_STAR_JUMP:
        line = (f"Sir, {label} just gained {gained} stars — it's at {stars:,} now.")
        await _fire(store, f"stars:{key}", line, kind="github")
        store.set_snapshot(key, {"stars": stars, "ts": time.time()})


async def _check_new_items(store: FeedsStore, key: str, label: str, items: list[dict] | None,
                           prefix: str) -> None:
    """reddit/news: alert on the newest unseen item; the FIRST SUCCESSFUL fetch only seeds 'seen'
    (no spam). A failed fetch (items is None) seeds nothing, so a later recovery isn't mistaken for a
    flood of 'new' items. ids are built ALIGNED with items (no zip-misalignment)."""
    if items is None:                                              # fetch failed — don't seed/alert
        return
    snap = store.get_snapshot(key)
    seen = set(snap.get("seen", [])) if isinstance(snap, dict) else None
    pairs = [(it, (it.get("id") or it.get("title") or "")[:120]) for it in items]
    ids = [i for _, i in pairs if i]
    if seen is None:                                               # first success — seed, don't alert
        store.set_snapshot(key, {"seen": ids[:50]})
        return
    fresh = [it for it, i in pairs if i and i not in seen]
    if fresh:
        top = fresh[0]
        title = (top.get("title") or "").strip()
        title = re.sub(r"\s+[-–|]\s+[\w .,&'/]{2,40}$", "", title).strip()[:160]   # drop " - CNBC" etc.
        line = f"{prefix} {label} — {title}, sir."
        await _fire(store, f"item:{key}:{(top.get('id') or top.get('title',''))[:80]}", line, kind="news")
    store.set_snapshot(key, {"seen": (ids + list(seen))[:50]})


async def _check_aqi(store: FeedsStore, city: str) -> None:
    async with httpx.AsyncClient(headers=sources.UA) as client:
        loc = await sources.geocode(client, city)
        if not loc:
            return
        aq = await sources.air_quality(client, loc["lat"], loc["lon"])
    if not aq:
        return
    key = f"aqi:{city.lower()}"
    snap = store.get_snapshot(key)
    was_bad = bool(snap.get("bad")) if isinstance(snap, dict) else False
    bad = aq["aqi"] >= FEEDS_AQI_BAD
    if bad and not was_bad:
        line = (f"Sir, the air in {loc['name']} has turned unhealthy — US AQI {aq['aqi']}.")
        await _fire(store, f"aqi:{key}", line, kind="aqi")
    store.set_snapshot(key, {"bad": bad, "aqi": aq["aqi"]})


async def _check_quake(store: FeedsStore, city: str) -> None:
    async with httpx.AsyncClient(headers=sources.UA) as client:
        loc = await sources.geocode(client, city)
        if not loc:
            return
        quakes = await sources.earthquakes(client, FEEDS_QUAKE_MAG)
    if quakes is None:                                              # FETCH failed — never seed/alert
        return                                                      # (else a blip would later scream)
    key = f"quake:{city.lower()}"
    snap = store.get_snapshot(key)
    seen = set(snap.get("seen", [])) if isinstance(snap, dict) else None
    near = [q for q in quakes
            if sources.haversine_km(loc["lat"], loc["lon"], q["lat"], q["lon"]) <= FEEDS_QUAKE_KM]
    near_ids = [q["id"] for q in near if q["id"]]
    if seen is None:                                                # first SUCCESS — seed, no backlog spam
        store.set_snapshot(key, {"seen": near_ids[:50]})
        return
    for q in near:
        if q["id"] and q["id"] not in seen:
            dist = int(sources.haversine_km(loc["lat"], loc["lon"], q["lat"], q["lon"]))
            line = (f"Sir — a magnitude {q['mag']:.1f} earthquake just struck about {dist} "
                    f"kilometres from {loc['name']} ({q['place']}).")
            await _fire(store, f"quake:{q['id']}", line, kind="quake", critical=True)
    store.set_snapshot(key, {"seen": (near_ids + list(seen))[:50]})


# ------------------------------------------------------------------ #
# One full sweep + the loop
# ------------------------------------------------------------------ #
async def check_all() -> int:
    """Run every watch once. Returns the number of alerts spoken (for tests/logs)."""
    store = get_feeds_store()
    watches = store.watches()
    if not watches:
        return 0
    before = len(_buffer)

    crypto = [w for w in watches if w.kind == "crypto"]
    async with httpx.AsyncClient(headers=sources.UA) as client:
        if crypto:
            prices = await sources.crypto_prices(client, [w.target for w in crypto])
            for w in crypto:
                p = prices.get(w.target)
                if p:
                    await _check_price(store, f"crypto:{w.target}", w.label, p["price"],
                                       w.threshold or FEEDS_MOVE_PCT, p["chg24h"])
        for w in watches:
            try:
                if w.kind == "stock":
                    q = await sources.stock_quote(client, w.target)
                    if q:
                        await _check_price(store, f"stock:{w.target}", w.label, q["price"],
                                           w.threshold or FEEDS_MOVE_PCT)
                elif w.kind == "github":
                    repo = await sources.github_repo(client, w.target)
                    if repo:
                        await _check_github(store, repo, w.label)
                elif w.kind == "reddit":
                    items = await sources.reddit_new(client, w.target, limit=8)
                    await _check_new_items(store, f"reddit:{w.target}", w.label, items, "Something new in")
                elif w.kind == "news":
                    items = await sources.news_search(client, w.target, limit=6)
                    await _check_new_items(store, f"news:{w.target.lower()}", w.label, items, "A fresh headline on")
            except Exception as e:  # noqa: BLE001 — one bad watch must not stop the sweep
                logger.debug("watch %s/%s failed: %s", w.kind, w.target, e)
    # AQI + quakes open their own client (need a geocode first)
    for w in watches:
        try:
            if w.kind == "aqi":
                await _check_aqi(store, w.target)
            elif w.kind == "quake":
                await _check_quake(store, w.target)
        except Exception as e:  # noqa: BLE001
            logger.debug("watch %s/%s failed: %s", w.kind, w.target, e)
    return len(_buffer) - before


async def monitor_loop() -> None:
    if not FEEDS_ENABLED:
        return
    logger.info("feeds monitor started (every %ds)", FEEDS_POLL_S)
    while True:
        try:
            await check_all()
        except Exception:  # noqa: BLE001
            logger.exception("feeds monitor sweep failed")
        await asyncio.sleep(FEEDS_POLL_S)


_task = None


def start_monitor() -> None:
    """Launch the background monitor (called once from the backend startup)."""
    global _task
    if not FEEDS_ENABLED or _task is not None:
        return
    try:
        _task = asyncio.create_task(monitor_loop())
    except RuntimeError:                          # no running loop (e.g. called from a sync context)
        logger.debug("no event loop yet — monitor will be started by the app startup hook")
