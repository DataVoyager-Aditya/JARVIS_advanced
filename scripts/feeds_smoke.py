"""
Phase 10.B smoke test — Real-time intelligence feeds.

Covers what's checkable without depending on a live network: the persistent store (watchlist +
snapshots + alert cooldown/dedup), the anomaly-detection logic for every kind (price move within a
rolling window, GitHub star jump, new-item seed-then-alert, quiet-hours hold vs critical bypass),
the haversine + RSS parsers, the facade + tools + router mount + persona. It ALSO does a couple of
best-effort LIVE fetches (CoinGecko price, a briefing) and reports them, but never fails on a network
blip — the deterministic logic above is the real guarantee.

Run:  python scripts/feeds_smoke.py    (use the project venv Python)
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "OK " if cond else "XX "
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {mark}{name}" + (f"  -- {detail}" if detail and not cond else ""))


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    from app.services.feeds.store import FeedsStore
    from app.services.feeds import sources, monitor
    from app.services import feeds
    from config import FEEDS_MOVE_WINDOW_S, FEEDS_STAR_JUMP

    print("\n[1] store: watchlist / snapshots / alert cooldown (persistent)")
    s = FeedsStore(tmp / "f1.db")
    wid = s.add_watch("crypto", "bitcoin", "Bitcoin", 5.0)
    check("add_watch returns id", wid > 0)
    check("re-add updates not duplicates", s.add_watch("crypto", "bitcoin", "BTC", 8.0) == wid and len(s.watches()) == 1)
    check("threshold updated on re-add", s.watches("crypto")[0].threshold == 8.0)
    s.add_watch("stock", "AAPL", "Apple")
    check("watches() lists all", len(s.watches()) == 2)
    check("watches(kind) filters", len(s.watches("stock")) == 1)
    check("remove_watch works", s.remove_watch("AAPL") == 1 and len(s.watches()) == 1)
    s.set_snapshot("k", {"price": 100, "ts": 123})
    check("snapshot roundtrips", s.get_snapshot("k") == {"price": 100, "ts": 123})
    check("missing snapshot is None", s.get_snapshot("nope") is None)
    check("alert not recent initially", not s.alert_recently("d1", 3600))
    s.record_alert("d1", "line")
    check("alert recent after record", s.alert_recently("d1", 3600))
    check("alert NOT recent beyond window", not s.alert_recently("d1", 0.0001))
    check("watchlist persists across reopen", len(FeedsStore(tmp / "f1.db").watches()) == 1)
    # REGRESSION #remove-watch: target-first, no label-collision across kinds
    rc = FeedsStore(tmp / "rc.db")
    rc.add_watch("stock", "TSLA", "Tesla")
    rc.add_watch("news", "Tesla", "Tesla")
    check("unwatch by target hits ONLY the target row (not the same-label stock)",
          rc.remove_watch("Tesla") == 1 and len(rc.watches()) == 1 and rc.watches()[0].kind == "stock")
    check("the TSLA stock is still removable by its own target", rc.remove_watch("TSLA") == 1)
    rc.add_watch("stock", "AAPL", "Apple")
    check("friendly-label removal works when unambiguous", rc.remove_watch("Apple") == 1 and not rc.watches())

    print("\n[2] parsers (offline)")
    d = sources.haversine_km(28.61, 77.20, 28.61, 77.20)
    check("haversine zero distance", d < 0.001)
    d2 = sources.haversine_km(28.61, 77.20, 19.07, 72.87)        # Delhi -> Mumbai ~1150km
    check("haversine Delhi->Mumbai ~1150km", 1000 < d2 < 1300, f"got {d2:.0f}")
    rss = sources._rss_titles("<rss><item><title>Hello &amp; bye</title></item>"
                              "<item><title type=\"html\">Second &#39;one&#39;</title></item></rss>", 5)
    check("REGRESSION #rss: decodes HTML entities + allows <title> attributes",
          [r["title"] for r in rss] == ["Hello & bye", "Second 'one'"], f"got {rss}")
    atom = sources._rss_titles("<feed><entry><title type=\"text\">Atom Title</title></entry></feed>", 5)
    check("REGRESSION #rss: Atom <entry> with a typed <title> parses", [r["title"] for r in atom] == ["Atom Title"])

    print("\n[3] facade: add/remove/list + invalid kind")
    import app.services.feeds.store as fstore
    fstore._store = FeedsStore(tmp / "facade.db")               # isolate the singleton
    check("facade add_watch ok", feeds.add_watch("crypto", "ethereum", "ETH")["ok"])
    check("facade rejects bad kind", not feeds.add_watch("banana", "x")["ok"])
    check("facade rejects empty target", not feeds.add_watch("crypto", "")["ok"])
    check("facade watches() reflects it", any(w.target == "ethereum" for w in feeds.watches()))
    check("facade remove_watch", feeds.remove_watch("ethereum") == 1)

    print("\n[4] tools + router + persona")
    from app.tools import discover, get
    discover()
    for tname in ("whats_happening", "market_check", "watch", "unwatch", "list_watches"):
        check(f"tool '{tname}' registered", get(tname) is not None)
    check("whats_happening is non-terminal (model phrases it)", not get("whats_happening").terminal)
    check("market_check is terminal", get("market_check").terminal)
    check("list_watches honest when empty", "empty" in get("list_watches").run({}).lower())
    out = get("watch").run({"kind": "crypto", "target": "solana", "threshold": 6})
    check("watch tool confirms", "watching" in out.lower() and "solana" in out.lower())
    check("watch tool rejects bad kind", "can" in get("watch").run({"kind": "xyz", "target": "z"}).lower())
    from app.main import app
    paths = {r.path for r in app.routes}
    for p in ("/feeds/alerts", "/feeds/briefing", "/feeds/dashboard", "/feeds/watchlist",
              "/feeds/watch", "/feeds/unwatch", "/feeds/market"):
        check(f"route {p} mounted", p in paths)
    from config import build_system_prompt
    sp = build_system_prompt()
    check("FEEDS persona block present", "KEEPING WATCH" in sp)

    # All async work (anomaly logic + live fetches) under ONE event loop (module Lock binds to it).
    asyncio.run(_async_tests(tmp))

    print(f"\n==== feeds (10.B) smoke: {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


async def _async_tests(tmp: Path) -> None:
    from app.services.feeds.store import FeedsStore
    from app.services.feeds import monitor, sources
    from app.services.feeds.briefing import briefing as briefing_fn, market_check as market_fn
    from config import FEEDS_MOVE_WINDOW_S, FEEDS_STAR_JUMP

    monitor._quiet_now = lambda: False                          # deterministic: speak in tests

    print("\n[5] anomaly: price move within a rolling window (#core)")
    st = FeedsStore(tmp / "mon.db")
    monitor._buffer.clear()
    await monitor._check_price(st, "crypto:bitcoin", "Bitcoin", 100.0, 5.0)
    check("first reading just baselines (no alert)", len(monitor._buffer) == 0)
    await monitor._check_price(st, "crypto:bitcoin", "Bitcoin", 94.0, 5.0)   # -6% >= 5%
    check("a >threshold move fires an alert", len(monitor._buffer) == 1 and "down" in monitor._buffer[0]["line"])
    monitor._buffer.clear()
    await monitor._check_price(st, "crypto:bitcoin", "Bitcoin", 93.5, 5.0)   # small move, on cooldown anyway
    check("repeat within cooldown stays silent", len(monitor._buffer) == 0)
    st2 = FeedsStore(tmp / "mon2.db")
    await monitor._check_price(st2, "stock:AAPL", "Apple", 200.0, 5.0)
    await monitor._check_price(st2, "stock:AAPL", "Apple", 203.0, 5.0)       # +1.5% < 5%
    check("sub-threshold move does NOT alert", len(monitor._buffer) == 0)
    st3 = FeedsStore(tmp / "mon3.db")
    st3.set_snapshot("crypto:eth", {"hist": [[time.time() - FEEDS_MOVE_WINDOW_S - 10, 100.0]]})
    await monitor._check_price(st3, "crypto:eth", "ETH", 90.0, 5.0)          # old sample falls out of window
    check("REGRESSION #FEEDS-MON-2: out-of-window sample pruned (no stale alert)", len(monitor._buffer) == 0)
    st4 = FeedsStore(tmp / "mon4.db")
    st4.set_snapshot("crypto:btc", {"hist": [[time.time() - 120, 100.0]]})   # a sample 2 min ago (in window)
    await monitor._check_price(st4, "crypto:btc", "BTC", 94.0, 5.0)          # -6% vs the OLDEST in-window sample
    check("REGRESSION #FEEDS-MON-2: rolling window measures vs oldest in-window sample",
          len(monitor._buffer) == 1)
    monitor._buffer.clear()

    print("\n[6] anomaly: github stars / new items / quiet-hold vs critical")
    monitor._buffer.clear()
    sg = FeedsStore(tmp / "gh.db")
    await monitor._check_github(sg, {"name": "o/r", "stars": 100}, "o/r")
    await monitor._check_github(sg, {"name": "o/r", "stars": 100 + FEEDS_STAR_JUMP}, "o/r")
    check("github star jump alerts", len(monitor._buffer) == 1 and "stars" in monitor._buffer[0]["line"])
    monitor._buffer.clear()
    sn = FeedsStore(tmp / "items.db")
    await monitor._check_new_items(sn, "reddit:py", "r/python", [{"id": "1", "title": "A"}], "New on")
    check("first run seeds, no alert", len(monitor._buffer) == 0)
    await monitor._check_new_items(sn, "reddit:py", "r/python",
                                   [{"id": "2", "title": "Fresh"}, {"id": "1", "title": "A"}], "New on")
    check("a new item alerts", len(monitor._buffer) == 1 and "Fresh" in monitor._buffer[0]["line"])
    # REGRESSION #FEEDS-MON-1: a FAILED first fetch (None) must NOT seed -> no flood on recovery
    monitor._buffer.clear()
    sf = FeedsStore(tmp / "fail.db")
    await monitor._check_new_items(sf, "reddit:rust", "r/rust", None, "New on")
    check("failed first fetch seeds nothing", sf.get_snapshot("reddit:rust") is None and len(monitor._buffer) == 0)
    await monitor._check_new_items(sf, "reddit:rust", "r/rust",
                                   [{"id": "x", "title": "pre-existing"}], "New on")
    check("first SUCCESS after a failure seeds silently (no flood)", len(monitor._buffer) == 0)
    # quiet-hours hold (non-critical) vs critical bypass
    monitor._buffer.clear()
    monitor._quiet_now = lambda: True
    sq = FeedsStore(tmp / "quiet.db")
    spoke = await monitor._fire(sq, "n1", "a normal alert", kind="market", critical=False)
    check("non-critical alert HELD in quiet hours (not spoken)", spoke is False and len(monitor._buffer) == 0)
    check("...but still logged for the dashboard", len(sq.recent_alerts()) == 1)
    spoke2 = await monitor._fire(sq, "c1", "a quake alert", kind="quake", critical=True)
    check("critical alert BYPASSES quiet hours", spoke2 is True and len(monitor._buffer) == 1)
    monitor._quiet_now = lambda: False

    print("\n[6c] REGRESSION #reddit-prefix + #quake-null-geometry (offline fakes)")
    class _Resp:
        def __init__(self, status=200, jd=None, text=""):
            self.status_code, self._jd, self.text = status, jd, text
        def json(self):
            return self._jd
    class _Fake:
        def __init__(self, resp, capture=None):
            self.resp, self.capture = resp, capture
        async def get(self, url, **kw):
            if self.capture is not None:
                self.capture.append(url)
            return self.resp
    cap: list = []
    await sources.reddit_new(_Fake(_Resp(200, {"data": {"children": []}}), cap), "rust")
    check("reddit prefix-strip keeps 'rust' (not 'ust')", any("/r/rust/" in u for u in cap), f"urls={cap}")
    cap2: list = []
    await sources.reddit_new(_Fake(_Resp(200, {"data": {"children": []}}), cap2), "r/python")
    check("reddit strips the 'r/' prefix only", any("/r/python/" in u for u in cap2))
    geo = {"features": [
        {"properties": {"mag": 5.0, "place": "x", "time": 0}, "geometry": None},                  # null geometry
        {"properties": {"mag": 5.5, "place": "y", "time": 0}, "geometry": {"coordinates": [10.0, 20.0]}, "id": "g"},
    ]}
    qs = await sources.earthquakes(_Fake(_Resp(200, geo)), 4.5)
    check("quake: a null-geometry feature doesn't drop the whole feed",
          qs is not None and len(qs) == 1 and qs[0]["id"] == "g", f"got {qs}")
    qs2 = await sources.earthquakes(_Fake(_Resp(500, None)), 4.5)
    check("quake: a fetch failure returns None (not [])", qs2 is None)

    print("\n[7] check_all with empty watchlist + drain")
    empty = FeedsStore(tmp / "empty.db")
    import app.services.feeds.store as fstore
    fstore._store = empty
    monitor._buffer.clear()
    n = await monitor.check_all()
    check("check_all no-ops on empty watchlist", n == 0)
    monitor._buffer.append({"line": "x", "ts": 0})
    check("drain returns + clears", monitor.drain() == ["x"] and monitor._buffer == [])

    print("\n[8] live (best-effort — network; never fails the smoke)")
    async with sources.httpx.AsyncClient(headers=sources.UA) as client:
        p = await sources.crypto_prices(client, ["bitcoin"])
    if p.get("bitcoin"):
        check("LIVE CoinGecko returns a BTC price", p["bitcoin"]["price"] > 0)
    else:
        print("     (CoinGecko unreachable — skipped live price check)")
    try:
        b = await briefing_fn()
        check("briefing returns a non-empty digest string", isinstance(b, str) and len(b) > 0)
        mc = await market_fn("bitcoin")
        check("market_check returns a sentence", isinstance(mc, str) and len(mc) > 0)
    except Exception as e:  # noqa: BLE001
        print(f"     (briefing/market live fetch failed, non-fatal: {e})")


if __name__ == "__main__":
    main()
