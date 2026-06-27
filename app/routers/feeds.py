"""
Feeds endpoints (Phase 10.B) — intel feeds, watchlist, alerts, briefing.

  GET  /feeds/alerts     desktop listener drains spoken anomaly-alert lines (like /messaging/announcements)
  GET  /feeds/briefing   the assembled live briefing digest (for the HUD / debug)
  GET  /feeds/dashboard  watchlist + recent alerts (for any HUD element that wants it)
  GET  /feeds/watchlist  current watchlist
  POST /feeds/watch      add a watch         {kind, target, label?, threshold?}
  POST /feeds/unwatch    remove a watch      {target, kind?}
  GET  /feeds/market     one-off quote       ?q=bitcoin

These are local/read-mostly (the listener + HUD), mirroring the other proactive surfaces.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from app.services import feeds

logger = logging.getLogger("jarvis.feeds.router")
router = APIRouter(prefix="/feeds", tags=["feeds"])


class WatchReq(BaseModel):
    kind: str
    target: str
    label: str = ""
    threshold: float = 0.0


class UnwatchReq(BaseModel):
    target: str
    kind: str | None = None


@router.get("/alerts")
async def alerts() -> dict:
    return {"lines": feeds.drain()}


@router.get("/briefing")
async def briefing() -> dict:
    try:
        return {"briefing": await feeds.briefing()}
    except Exception:  # noqa: BLE001
        logger.exception("briefing failed")
        return {"briefing": ""}


@router.get("/dashboard")
async def dashboard() -> dict:
    store = feeds.get_feeds_store()
    return {
        "watchlist": [{"kind": w.kind, "target": w.target, "label": w.label,
                       "threshold": w.threshold} for w in store.watches()],
        "alerts": store.recent_alerts(10),
    }


@router.get("/watchlist")
async def watchlist() -> dict:
    return {"watchlist": [{"kind": w.kind, "target": w.target, "label": w.label,
                           "threshold": w.threshold} for w in feeds.watches()]}


@router.post("/watch")
async def watch(req: WatchReq) -> dict:
    return feeds.add_watch(req.kind, req.target, req.label, req.threshold)


@router.post("/unwatch")
async def unwatch(req: UnwatchReq) -> dict:
    n = feeds.remove_watch(req.target, req.kind)
    return {"ok": n > 0, "removed": n}


@router.get("/market")
async def market(q: str = "") -> dict:
    return {"text": await feeds.market_check(q)}
