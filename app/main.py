"""
JARVIS_advanced — FastAPI application entrypoint.

Phase 1 mounts the voice router (STT / TTS / full-duplex converse). Later phases mount
their own routers here.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import ASSISTANT_NAME, GROQ_API_KEYS, BASE_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("jarvis")

app = FastAPI(title=f"{ASSISTANT_NAME} backend", version="1.0-phase1")

# Open CORS — clients are local (PC listener) and later the PWA over the tunnel.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "assistant": ASSISTANT_NAME,
        "groq_keys": len(GROQ_API_KEYS),
    }


@app.on_event("startup")
async def _startup() -> None:
    logger.info("%s backend online — %d Groq key(s) loaded", ASSISTANT_NAME, len(GROQ_API_KEYS))

    # The backend is the single brain: warm the memory layer ONCE here (off-thread so it
    # doesn't block serving) and run the nightly consolidator. The voice listener delegates
    # thinking to /chat, so memory/embeddings never load a second time.
    import asyncio
    from config import MEMORY_ENABLED, MESSAGING_ENABLED, EMOTION_ENABLED

    # All model warm-ups share one task and run SEQUENTIALLY. Memory embeddings, emotion and
    # voice-tone all do their FIRST import of transformers/huggingface_hub; doing that from
    # several threads at once partial-fails with ImportError (the module is half-initialised when
    # the next thread grabs it). So we import the heavy libs once in a single thread, THEN warm
    # each model in turn — off the hot path, never blocking request serving.
    if MEMORY_ENABLED or EMOTION_ENABLED:
        async def _warm_models() -> None:
            def _preimport():
                try:
                    import transformers  # noqa: F401
                    import sentence_transformers  # noqa: F401
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.to_thread(_preimport)        # complete the heavy import ONCE, alone

            if MEMORY_ENABLED:
                try:
                    from app.services.memory import get_memory
                    mem = await asyncio.to_thread(get_memory)
                    mem.start_consolidation(hour=3, minute=0)
                    logger.info("memory warmed — %s", mem.stats())
                except Exception:  # noqa: BLE001
                    logger.exception("memory warm-up failed")

            if EMOTION_ENABLED:
                try:
                    import app.services.emotion as emotion
                    from app.services.emotion.voice import get_voice_emotion
                    await asyncio.to_thread(emotion.warm)              # text emotion (distilroberta)
                    await asyncio.to_thread(get_voice_emotion().warm)  # voice tone (wav2vec2 SER)
                    logger.info("emotion models warmed (text + voice tone)")
                except Exception:  # noqa: BLE001
                    logger.warning("emotion warm-up skipped")
        asyncio.create_task(_warm_models())

    # Phase 10.B — start the intel feeds monitor (watchlist anomaly detection). No-ops if the
    # watchlist is empty; each source fails soft, so this never blocks or crashes serving.
    from config import FEEDS_ENABLED
    if FEEDS_ENABLED:
        from app.services import feeds
        feeds.start_monitor()

    # Phase 10.A — start the deep-research topic monitor (daily re-run of "keep watching X" topics).
    # No-ops if nothing is being watched; sweeps run on a dedicated worker thread so they never block.
    from config import RESEARCH_ENABLED
    if RESEARCH_ENABLED:
        from app.services import research
        research.start_monitor()

    # Phase 7 — start the messaging pollers (email + Instagram). WhatsApp is push-based
    # (the sidecar calls our webhook), so it needs no poller. Each loop no-ops if its
    # channel isn't configured, so this is safe to always call.
    if MESSAGING_ENABLED:
        from app.services.messaging.pollers import start_pollers
        start_pollers()

        # Pre-warm the messaging clients off-thread so the FIRST command isn't cold: validating
        # the saved Instagram session (account_info) and nudging the WhatsApp sidecar to warm its
        # chat cache otherwise cost a few seconds on first use. Best-effort — never blocks serving.
        async def _warm_messaging() -> None:
            from app.services.messaging.instagram import get_instagram
            from app.services.messaging.whatsapp_client import get_whatsapp
            try:
                await asyncio.to_thread(get_instagram().status)
            except Exception:  # noqa: BLE001
                pass
            try:
                await get_whatsapp().status()
            except Exception:  # noqa: BLE001
                pass
        asyncio.create_task(_warm_messaging())


# --- routers ---
from app.routers import voice as voice_router  # noqa: E402
from app.routers import admin as admin_router  # noqa: E402
from app.routers import web as web_router  # noqa: E402
from app.routers import events as events_router  # noqa: E402
from app.routers import messaging as messaging_router  # noqa: E402
from app.routers import vision as vision_router  # noqa: E402
from app.routers import calls as calls_router  # noqa: E402
from app.routers import identity as identity_router  # noqa: E402
from app.routers import proactive as proactive_router  # noqa: E402
from app.routers import feeds as feeds_router  # noqa: E402
from app.routers import research as research_router  # noqa: E402

# Static assets for the PWA (built from JARVIS.html via scripts/build_pwa.py).
_WEB_STATIC = BASE_DIR / "app" / "web" / "static"
if _WEB_STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_WEB_STATIC)), name="static")

app.include_router(voice_router.router)
app.include_router(admin_router.router)
app.include_router(web_router.router)
app.include_router(events_router.router)
app.include_router(messaging_router.router)
app.include_router(vision_router.router)
app.include_router(calls_router.router)
app.include_router(identity_router.router)
app.include_router(proactive_router.router)
app.include_router(feeds_router.router)
app.include_router(research_router.router)
