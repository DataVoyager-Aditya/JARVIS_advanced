# Deferred backlog — things consciously NOT built yet (so nothing is lost)

Per the finality rule, when something can't be built fully + for free *right now*, we defer the
**whole** feature rather than ship a stub — and record it here. Each item says **where it gets
picked up**. This is the master "don't forget" list; [STATUS.md](STATUS.md) tracks live progress.

Nothing here is abandoned. It's queued.

---

## Deferred during Phase 6 (tools/agent)

| Item | Why deferred | Picked up in |
|------|--------------|--------------|
| Messaging tools — send/read WhatsApp, Telegram, Instagram, email; unified inbox | Needs the messaging integrations (sidecar + logins) | **Phase 7** |
| Vision tools — `describe_image`, `read_screen` ("what's on my screen / what is this") | Needs the vision pipeline | **Phase 2** |
| Calendar / tasks — Google Calendar, Outlook, Notion add/read | Needs per-service OAuth setup (free, but a setup step) | Later (after P7), tracked here |
| **Real coffee / smart-home** — control a smart plug / lights / devices | Needs a cheap Wi-Fi smart plug + local device API (Tuya/Kasa/Home Assistant) | **Phase 10.E** |
| `run_python` — execute code on request | Security surface; must be properly sandboxed first | Later (with sandbox), tracked here |
| News brief tool | Optional; pick a free RSS/GNews source | Anytime, tracked here |

## Deferred during Phase 7 (messaging)

| Item | Why deferred | Picked up in |
|------|--------------|--------------|
| **WhatsApp calling** (place a voice/video call to a person/group) | `whatsapp-web.js` has no call API — calling isn't part of the WhatsApp Web protocol. The only programmatic path is paid telephony/SIP, which breaks the zero-cost rule | Only if a free path appears; messaging + groups are fully done |
| **Telegram** integration | Aditya chose to drop it ("don't wanna automate telegram") — he uses it manually | Not planned (can revisit if he asks) |
| Email auto-send to non-whitelisted recipients | Kept draft-and-notify by design so the inbox is never mass-sent by accident; `reply_to_messages` is WhatsApp/Instagram only | By design — opt-in via `GMAIL_AUTOSEND_WHITELIST` |

## Deferred / traded earlier

| Item | Why | Revisit |
|------|-----|---------|
| One-breath wake command via Vosk ("wake up jarvis, <cmd>") | Traded for **reliable** grammar-constrained wake detection (Phase 1) | Optional polish — only if wanted; current 2-stage is solid |
| Custom-trained "wake up jarvis" model (lighter CPU than Vosk) | Vosk grammar works well + free; training was heavier | Optional, only if CPU ever matters |
| More free backup LLM keys (Gemini exhausted; Cerebras/Together/Mistral empty) | Just need keys added to `.env` (no code) | Whenever Aditya adds free keys — they auto-activate |
| ElevenLabs premium voice (optional upgrade over Edge-TTS) | Edge-TTS Ryan is free + good; Eleven needs keys | Optional, `.env` keys + `JARVIS_TTS_ENGINE=elevenlabs` |

## Big features still ahead (full phases, already in PLANNER.md)

- **Phase 2** — Vision
- **Phase 4** — 3-tier memory + knowledge graph (next per planner order)
- **Phase 5** — Emotion / humor / personalization
- **Phase 7** — Messaging
- **Phase 8** — Calls (Android bridge)
- **Phase 9** — PWA + Cloudflare tunnel
- **Phase 10** — JARVIS-grade autonomy (research, smart-home/coffee, gesture, proactive, etc.)
- **Phase 11** — Identity / access control

> Rule reminder: building a *deferred* item later is NOT "polishing a shipped feature" — it was
> never built. The no-going-back rule only protects features we already completed.
