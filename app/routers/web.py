"""
Web / PWA endpoints (Phase 9).

Serves the installable JARVIS PWA (built from JARVIS.html by scripts/build_pwa.py) and the
text-chat bridge the UI calls. Voice (mic -> STT, reply -> TTS) reuses the existing
/voice/stt and /voice/tts/stream endpoints; this module adds:

  GET  /                      the app shell (index.html)
  GET  /manifest.webmanifest  PWA manifest
  GET  /sw.js                 service worker (served at root so its scope is the whole app)
  POST /chat                  {text, session_id} -> {reply, module}  (full agent: tools + memory)

Every PWA turn is tagged channel="pwa_chat" so memory is shared across surfaces (the Phase 4
per-channel requirement: "you mentioned this on WhatsApp earlier").
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from config import BASE_DIR, MEMORY_ENABLED
from app.services.agent import get_agent
from app.routers.events import broadcast

logger = logging.getLogger("jarvis.web")
router = APIRouter(tags=["web"])

WEB_DIR = BASE_DIR / "app" / "web"

# Per-session working memory (Tier 1) for the PWA. Durable memory lives in the Phase 4 stores.
_sessions: dict[str, list[dict]] = {}

# Map the tool that ran -> which HUD side-panel lights up, so the visuals track real actions.
_MODULE_FOR_TOOL = {
    "web_search": "feeds", "get_weather": "feeds",
    "recall": "memory", "remember": "memory",
    "note_write": "memory", "note_search": "memory", "note_today": "memory",
    "list_reminders": "inbox", "set_timer": "inbox", "set_reminder": "inbox",
    "cancel_reminder": "inbox", "stop_alarm": "inbox", "snooze_alarm": "inbox",
    "system_status": "system", "set_volume": "system", "media_control": "system",
    "take_screenshot": "vision",
    "open_app": "agents", "open_url": "agents", "play_youtube": "agents",
    "save_routine": "agents", "run_routine": "agents", "list_routines": "agents",
}


def _module_from_trace(trace) -> str | None:
    for t in trace or []:
        m = _MODULE_FOR_TOOL.get(getattr(t, "name", None))
        if m:
            return m
    return None


class ChatReq(BaseModel):
    text: str
    session_id: str | None = None
    channel: str = "pwa_chat"          # pc_voice when the desktop listener delegates here
    voice_emotion: dict | None = None  # Phase 5: {emotion,intensity,scores} from the speech-tone model
    speaker_tier: str | None = None    # Phase 11: verified speaker tier (owner|trusted|guest|stranger)
    speaker_name: str = ""             # Phase 11: who the voice was matched to (display name)


def _trust_for(req: "ChatReq"):
    """Build the Phase-11 Trust for this turn.

    - The PC voice listener passes a verified speaker_tier (local, trusted) -> use it.
    - Remote surfaces (the phone) DON'T self-report a tier; instead the server verified their
      voice via /identity/whoami and stashed it per session. We read that here. It is
      DOWNGRADE-ONLY: a recognised friend/stranger gets their (lower) tier, but the absence of a
      match never locks the Owner out (defaults to Owner) — recognition restricts, never blocks.
    The passphrase is checked HERE against the spoken text (single source of truth)."""
    from app.services import identity
    if req.speaker_tier:
        t = identity.Trust(tier=req.speaker_tier, name=req.speaker_name or "",
                           display=req.speaker_name or "", source="voice")
        if t.is_owner and req.text:
            try:
                t.passphrase_ok = identity.get_store().check_passphrase(req.text)
            except Exception:  # noqa: BLE001
                pass
        return t
    # remote surface: use the server-verified session tier only to RESTRICT a recognised non-owner.
    if identity.enabled() and req.session_id:
        try:
            st = identity.get_session_trust(req.session_id)
        except Exception:  # noqa: BLE001
            st = None
        if st is not None and st.tier != "owner":
            return st
    return None


def _set_active_identity(trust) -> None:
    """Mark who's using JARVIS now and push it to the HUD/mobile name panel (fire-and-forget)."""
    if trust is None:
        return
    try:
        from app.services import identity
        identity.set_active(trust)
        asyncio.create_task(broadcast({"type": "identity", **identity.active_view()}))
    except Exception:  # noqa: BLE001
        pass


@router.post("/chat")
async def chat(req: ChatReq):
    text = (req.text or "").strip()
    if not text:
        return {"reply": "", "module": None, "sleep": False}
    sid = req.session_id or "default"
    channel = req.channel or "pwa_chat"
    trust = _trust_for(req)
    _set_active_identity(trust)
    history = _sessions.setdefault(sid, [])
    reply = await get_agent().run(text, history=history, channel=channel, trust=trust)
    full = (reply.text or "").strip()
    if full:
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": full})
        _sessions[sid] = history[-20:]
        # Only the Owner's turns enter his private memory — never a trusted/guest/unknown speaker.
        if MEMORY_ENABLED and (trust is None or trust.is_owner):
            from app.services.memory import get_memory
            asyncio.create_task(asyncio.to_thread(get_memory().log_turn, text, full, channel))
    return {"reply": full, "module": _module_from_trace(reply.trace), "sleep": reply.sleep}


@router.post("/chat/stream")
async def chat_stream(req: ChatReq):
    """Streaming variant for the voice listener: emits newline-delimited JSON — a `narrate`
    line the moment a tool starts (so JARVIS can say "Searching the web…" aloud while it
    runs), then a final `reply` line. Also mirrors narration to the HUD via the event bus."""
    text = (req.text or "").strip()
    sid = req.session_id or "default"
    channel = req.channel or "pwa_chat"

    async def empty():
        yield json.dumps({"type": "reply", "reply": "", "sleep": False}) + "\n"
    if not text:
        return StreamingResponse(empty(), media_type="application/x-ndjson")

    history = _sessions.setdefault(sid, [])
    trust = _trust_for(req)
    _set_active_identity(trust)
    q: asyncio.Queue = asyncio.Queue()
    _SENT = object()

    async def narrate(line: str) -> None:
        await q.put(line)
        await broadcast({"type": "narrate", "text": line})   # HUD shows it too

    async def run_agent():
        try:
            return await get_agent().run(text, history=history, narrate=narrate, channel=channel,
                                         voice_emotion=req.voice_emotion, trust=trust)
        except Exception:  # noqa: BLE001
            logger.exception("chat/stream agent failed")
            from app.services.agent.runner import AgentReply
            return AgentReply("I couldn't reach my services just now, sir.", [], 0)
        finally:
            await q.put(_SENT)

    async def gen():
        task = asyncio.create_task(run_agent())
        while True:
            item = await q.get()
            if item is _SENT:
                break
            yield json.dumps({"type": "narrate", "text": item}) + "\n"
        reply = await task
        full = (reply.text or "").strip()
        if full:
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": full})
            _sessions[sid] = history[-20:]
            if MEMORY_ENABLED and (trust is None or trust.is_owner):
                from app.services.memory import get_memory
                asyncio.create_task(asyncio.to_thread(get_memory().log_turn, text, full, channel))
        # Phase 5 — push the live mood to the HUD so the EMOTION panel reflects his real register.
        if getattr(reply, "mood", None):
            await broadcast({"type": "mood", **reply.mood})
        yield json.dumps({"type": "reply", "reply": full, "register": (reply.mood or {}).get("register"),
                          "prosody": (reply.mood or {}).get("prosody"),
                          "module": _module_from_trace(reply.trace), "sleep": reply.sleep}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# --------------------------------------------------------------------------- #
# GET /ticker — live HUD ticker (free, no-key sources, cached 60s)
# --------------------------------------------------------------------------- #
_CYAN, _AMBER, _GREEN, _RED = "#7df0ff", "#ffb24d", "#4dffb0", "#ff6b6b"
_ticker_cache: dict = {"at": 0.0, "items": []}


def _user_city() -> str:
    # Cheap direct read — must NOT build the full memory service (which loads embeddings).
    try:
        import sqlite3
        from config import MEMORY_DB
        if MEMORY_DB.exists():
            con = sqlite3.connect(MEMORY_DB)
            row = con.execute("SELECT value FROM facts WHERE key='user.location'").fetchone()
            con.close()
            if row and row[0]:
                return row[0].split(",")[0].strip()
    except Exception:  # noqa: BLE001
        pass
    return "New Delhi"


async def _crypto(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": "bitcoin,ethereum", "vs_currencies": "usd",
                                 "include_24hr_change": "true"}, timeout=6)
    d = r.json()
    out = []
    for tag, key in (("BTC", "bitcoin"), ("ETH", "ethereum")):
        if key in d:
            price = d[key]["usd"]
            chg = d[key].get("usd_24h_change", 0.0)
            sign = "+" if chg >= 0 else ""
            out.append({"tag": tag, "c": _GREEN if chg >= 0 else _RED,
                        "text": f"${price:,.0f}  {sign}{chg:.1f}%"})
    return out


async def _weather(client: httpx.AsyncClient, city: str) -> list[dict]:
    g = (await client.get("https://geocoding-api.open-meteo.com/v1/search",
                          params={"name": city, "count": 1}, timeout=6)).json()
    if not g.get("results"):
        return []
    loc = g["results"][0]
    w = (await client.get("https://api.open-meteo.com/v1/forecast", timeout=6, params={
        "latitude": loc["latitude"], "longitude": loc["longitude"], "current": "temperature_2m"})).json()
    t = w["current"]["temperature_2m"]
    return [{"tag": "WX", "c": _CYAN, "text": f"{loc['name']} {round(t)}°C"}]


async def _hn(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get("https://hn.algolia.com/api/v1/search",
                         params={"tags": "front_page"}, timeout=6)
    hits = r.json().get("hits", [])[:2]
    return [{"tag": "HN", "c": _AMBER, "text": h.get("title", "")[:70]} for h in hits if h.get("title")]


async def _world_news(client: httpx.AsyncClient) -> list[dict]:
    import re as _re
    r = await client.get("https://feeds.bbci.co.uk/news/world/rss.xml", timeout=6)
    titles = _re.findall(r"<item>.*?<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", r.text, _re.DOTALL)
    return [{"tag": "NEWS", "c": _CYAN, "text": t.strip()[:70]} for t in titles[:2] if t.strip()]


@router.get("/ticker")
async def ticker():
    now = time.time()
    if now - _ticker_cache["at"] < 60 and _ticker_cache["items"]:
        return {"items": _ticker_cache["items"]}
    items: list[dict] = []
    async with httpx.AsyncClient(headers={"User-Agent": "JARVIS/1.0"}) as client:
        results = await asyncio.gather(
            _crypto(client), _hn(client), _world_news(client), _weather(client, _user_city()),
            return_exceptions=True,
        )
    for r in results:
        if isinstance(r, list):
            items.extend(r)
        else:
            logger.warning("ticker source failed: %s", r)
    if items:
        _ticker_cache.update(at=now, items=items)
    elif _ticker_cache["items"]:
        items = _ticker_cache["items"]            # serve stale rather than empty
    return {"items": items}


@router.get("/memory/graph")
async def memory_graph():
    """Real data for the HUD's MEMORY GRAPH panel: a centre node + up to 5 neighbours, from
    the knowledge graph if populated, else an ego-graph of the user's real ventures/people."""
    if not MEMORY_ENABLED:
        return {"center": "MEMORY", "nodes": [], "stat": "OFFLINE"}
    from app.services.memory import get_memory
    mem = get_memory()
    _ents, rels = mem.graph.counts()
    facts = mem.semantic.count()
    stat = f"3-TIER · {facts}"

    def fmt(s: str) -> str:
        return s.replace("_", " ").strip().upper()[:10]

    # 1) knowledge graph (filled by nightly consolidation)
    if rels > 0:
        center, neighbours = await asyncio.to_thread(mem.graph.ego_graph, 5)
        if center:
            return {"center": fmt(center), "nodes": [fmt(n) for n in neighbours], "stat": stat}

    # 2) fallback: ego-graph from the profile — the user's actual ventures + key people
    center = (mem.semantic.get("user.preferred_name") or "MEMORY").upper()
    nodes: list[str] = []
    seen = set()
    for f in mem.semantic.all():
        ent = None
        if f.key.startswith(("startup.", "organization.")):
            ent = f.key.split(".")[1]
        elif f.key.startswith("project."):
            ent = f.key.split(".")[1]
        if ent and ent not in seen:
            seen.add(ent)
            nodes.append(fmt(ent))
        if len(nodes) >= 5:
            break
    # pad with key people if we have room
    if len(nodes) < 5:
        for f in mem.semantic.all():
            if f.key.startswith("contacts.") and f.key.endswith(".relation"):
                name = f.key.split(".")[1]
                if name not in seen:
                    seen.add(name)
                    nodes.append(fmt(name))
            if len(nodes) >= 5:
                break
    return {"center": center[:10], "nodes": nodes, "stat": stat}


@router.get("/")
async def index(request: Request):
    # Phones get the dedicated phone UI (app/web/mobile.html); desktop gets the unchanged HUD
    # (index.html) — byte-for-byte identical, so the PC experience never shifts. `?ui=mobile`
    # or `?ui=desktop` force either, which is handy for previewing the phone UI on a desktop.
    ui = (request.query_params.get("ui") or "").lower()
    ua = (request.headers.get("user-agent") or "").lower()
    is_phone = any(t in ua for t in ("mobile", "android", "iphone", "ipad", "ipod", "silk"))
    # no-store so a device never keeps serving the wrong shell from cache (the shells are tiny and
    # the service worker is network-first anyway) — this is the only thing that makes the phone vs
    # desktop choice take effect immediately on every load.
    no_store = {"Cache-Control": "no-store, must-revalidate"}
    if (ui == "mobile" or (ui != "desktop" and is_phone)):
        mobile = WEB_DIR / "mobile.html"
        if mobile.exists():
            return FileResponse(mobile, media_type="text/html", headers=no_store)
    f = WEB_DIR / "index.html"
    if not f.exists():
        return JSONResponse({"detail": "PWA not built — run: python scripts/build_pwa.py"}, status_code=503)
    return FileResponse(f, media_type="text/html", headers=no_store)


@router.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@router.get("/sw.js")
async def service_worker():
    # Served from root so the worker can control the whole "/" scope.
    return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})
