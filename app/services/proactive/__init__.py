"""
Phase 10.F — proactive & predictive intelligence (facade).

JARVIS speaks up on his own when it's earned — routine nudges, a check-in, a quiet remark about
his work, a hydration prompt, "you haven't called Mom in a while". The listener polls `poll(state)`;
the engine decides whether/what, and tools use `pause`/`resume`/`status` to control it.
"""

from __future__ import annotations

from app.services.proactive.engine import ProactiveEngine, get_engine
from app.services.proactive.store import ProactiveStore, get_proactive_store


async def poll(state: dict) -> dict:
    return await get_engine().poll(state)


def ack(kind: str, key: str = "") -> None:
    """Confirm a polled line was actually spoken — only now does it count toward the cap/gap/dedup."""
    get_engine().record(kind, key)


def pause(minutes: int) -> int:
    return get_engine().pause(minutes)


def resume() -> None:
    get_engine().resume()


def status() -> dict:
    return get_engine().status()


__all__ = ["ProactiveEngine", "get_engine", "ProactiveStore", "get_proactive_store",
           "poll", "ack", "pause", "resume", "status"]
