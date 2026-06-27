"""
Live event bus (Phase 9) — lets the always-on desktop voice listener drive the PWA HUD.

The headless `jarvis_listener.py` is the proven hands-free voice engine (Vosk wake word +
Whisper + Edge TTS). It POSTs its state here as it runs (listening / transcript / reply /
idle); the backend fan-outs each event to every connected PWA over a WebSocket, so the HUD
animates in real time while JARVIS speaks through the PC — no clicking, no browser mic.

  WS   /events/ws        PWA subscribes; receives JSON events
  POST /events/publish   listener pushes an event {type, ...} -> broadcast to all PWAs
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("jarvis.events")
router = APIRouter(prefix="/events", tags=["events"])

_clients: set[WebSocket] = set()
_lock = asyncio.Lock()


async def broadcast(event: dict) -> int:
    """Send an event to every connected PWA. Returns how many got it."""
    if not _clients:
        return 0
    dead = []
    async with _lock:
        targets = list(_clients)
    for ws in targets:
        try:
            await ws.send_json(event)
        except Exception:  # noqa: BLE001
            dead.append(ws)
    if dead:
        async with _lock:
            for ws in dead:
                _clients.discard(ws)
    return len(targets) - len(dead)


@router.post("/publish")
async def publish(event: dict) -> dict:
    n = await broadcast(event)
    return {"ok": True, "delivered": n}


@router.websocket("/ws")
async def events_ws(ws: WebSocket) -> None:
    await ws.accept()
    async with _lock:
        _clients.add(ws)
    logger.info("PWA connected to event bus (%d total)", len(_clients))
    try:
        while True:
            # We don't expect messages from the PWA; this just detects disconnect.
            await ws.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        async with _lock:
            _clients.discard(ws)
        logger.info("PWA disconnected from event bus (%d left)", len(_clients))
