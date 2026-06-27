"""Admin / introspection endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from app.services.llm.key_rotator import get_rotator
from config import MEMORY_ENABLED

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/key-stats")
async def key_stats() -> dict:
    """Per-provider, per-key quota usage for today + breaker state."""
    return get_rotator().stats()


@router.get("/memory")
async def memory_stats() -> dict:
    """Phase 4 — counts across the 3 memory tiers + knowledge graph."""
    if not MEMORY_ENABLED:
        return {"enabled": False}
    from app.services.memory import get_memory
    return {"enabled": True, **get_memory().stats()}


@router.post("/memory/consolidate")
async def memory_consolidate(hours: float = 24.0) -> dict:
    """Run the nightly distillation on demand (summary + facts + triples)."""
    if not MEMORY_ENABLED:
        return {"enabled": False}
    from app.services.memory import get_memory
    return await get_memory().consolidate_now(hours)
