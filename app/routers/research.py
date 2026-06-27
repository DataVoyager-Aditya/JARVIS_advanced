"""
Deep-research endpoints (Phase 10.A).

  GET  /research/progress   listener drains spoken progress lines (no chime — "still working")
  GET  /research/done       listener drains finished-briefing announcements (spoken with a soft chime)
  GET  /research/status     what's running + last finished briefing
  POST /research/start      kick off a sweep            {topic}
  GET  /research/briefing   read a saved briefing       ?topic=  (empty = latest)
  GET  /research/dashboard  monitors + recent briefings (for the HUD)
  POST /research/watch      add a topic monitor         {topic}
  POST /research/unwatch    remove a topic monitor      {topic}

Local/read-mostly, mirroring the feeds + proactive surfaces.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from app.services import research

logger = logging.getLogger("jarvis.research.router")
router = APIRouter(prefix="/research", tags=["research"])


class StartReq(BaseModel):
    topic: str


class TopicReq(BaseModel):
    topic: str


@router.get("/progress")
async def progress() -> dict:
    return {"lines": research.drain_progress()}


@router.get("/done")
async def done() -> dict:
    return {"items": research.drain_done()}


@router.get("/status")
async def status() -> dict:
    return research.research_status()


@router.post("/start")
async def start(req: StartReq) -> dict:
    return research.start_research(req.topic)


@router.get("/briefing")
async def briefing(topic: str = "") -> dict:
    b = research.get_briefing(topic) if topic.strip() else research.latest_briefing()
    if not b:
        return {"found": False}
    return {"found": True, "topic": b.title, "summary": b.summary, "full_md": b.full_md,
            "confidence": b.confidence, "sources": b.sources, "ts": b.ts}


@router.get("/dashboard")
async def dashboard() -> dict:
    return {
        "monitors": [{"topic": m.label, "interval_h": m.interval_h, "last_run_ts": m.last_run_ts}
                     for m in research.topics()],
        "briefings": [{"topic": b.title, "summary": b.summary, "confidence": b.confidence,
                       "sources": len(b.sources or []), "ts": b.ts}
                      for b in research.list_briefings(12)],
        "status": research.research_status(),
    }


@router.post("/watch")
async def watch(req: TopicReq) -> dict:
    return research.watch_topic(req.topic)


@router.post("/unwatch")
async def unwatch(req: TopicReq) -> dict:
    n = research.unwatch_topic(req.topic)
    return {"ok": n > 0, "removed": n}
