# Friday — Advanced JARVIS Upgrade

## Context

Today's project at [c:\Users\Lenovo\Desktop\JARVIS](c:\Users\Lenovo\Desktop\JARVIS) is a text-only FastAPI chat backend powered by Groq (Llama-3.3-70B), with FAISS vector memory, Tavily web search, and a 6-key Groq rotation in [app/services/groq_service.py](app/services/groq_service.py). No voice, no vision, no messaging, no UI.

The user (Aditya) wants to transform it into **"Friday"** — the Iron-Man-style assistant: voice-driven, vision-capable, emotionally aware, deeply personalized, reachable from PC + phone + web, integrated with WhatsApp / Telegram / Email / Calls, and built entirely on **free APIs without credit cards** using the same multi-key rotation trick already proven with Groq.

User decisions locked in:
- **Voice:** Cloud-first (Groq Whisper STT + ElevenLabs/Edge-TTS), keys rotated.
- **WhatsApp:** `whatsapp-web.js` (unofficial, free, instant).
- **Calls:** Notify on incoming, list missed, sometimes auto-handle — via Android companion (Macrodroid or tiny Kotlin app) bridging to Friday's API.
- **Reach:** PWA + Cloudflare Tunnel for PC + phone + web.
- **Memory:** Keep + heavily extend existing FAISS foundation.
- **Always-on (required):** JARVIS auto-starts on PC boot and phone unlock, runs **fully headless in the background** (no window/UI needed — a console-less service on PC, a foreground service on Android), and is used as an **installed app, not a browser tab**. Opening the app attaches to the already-running background JARVIS. Full spec in **Phase 10.L**.

The build is delivered in **10 incremental phases**, each independently shippable.

---

## High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  CLIENTS                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐           │
│  │  PWA (PC +   │  │  Telegram    │  │  Android     │           │
│  │  phone, web) │  │  bot         │  │  companion   │           │
│  │  mic+camera  │  │  voice/img   │  │  call bridge │           │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘           │
│         └─────────────────┴─────────────────┘                    │
│                           │ HTTPS via Cloudflare Tunnel          │
└───────────────────────────┼──────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  FRIDAY BACKEND (FastAPI, extends current app/)                  │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  Orchestrator (agent loop, tool calling, streaming)        │ │
│  └─┬─────────┬─────────┬─────────┬─────────┬─────────┬────────┘ │
│    │         │         │         │         │         │          │
│  ┌─▼──┐  ┌──▼──┐  ┌───▼──┐  ┌───▼───┐ ┌──▼───┐  ┌──▼────┐     │
│  │STT │  │ TTS │  │Vision│  │Memory │ │Tools │  │Skills │     │
│  └─┬──┘  └──┬──┘  └───┬──┘  └───┬───┘ └──┬───┘  └──┬────┘     │
│    │       │          │        │        │         │            │
│  ┌─▼───────▼──────────▼────────▼────────▼─────────▼──────────┐ │
│  │  KeyRotator — multi-provider, quota-aware                 │ │
│  │  Groq · Gemini · Together · OpenRouter · HF · Cerebras    │ │
│  │  Mistral · ElevenLabs · Deepgram                          │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  Sidecars: whatsapp-web.js (Node), Gmail poller (Python)        │
└──────────────────────────────────────────────────────────────────┘
```

---

## Phase 1 — Friday persona + streaming voice + wake word + interruption

**Goal:** Talk to her by voice from your PC. She wakes on "Hey Friday," replies in a Friday-like voice, and you can cut her off mid-sentence.

- Rewrite system prompt in [config.py:146](config.py#L146) as **Friday** persona — younger, warmer, sassier than JARVIS; Irish-tinged; addresses you as "Boss"; emotional intelligence; calls you out playfully.
- New module `app/services/voice/`:
  - `stt.py` — Groq Whisper (`whisper-large-v3-turbo`, free, ~$0 with key rotation) primary; `faster-whisper` (local, CPU) fallback.
  - `tts.py` — ElevenLabs (rotated keys, ~10k chars/key/month free) primary; `edge-tts` (Microsoft, unlimited free, no key) fallback.
  - `wake_word.py` — `openwakeword` (free, local, ONNX). Primary phrase **"Wakeup Friday"**, plus aliases "Hey Friday", "Friday". Custom-trained ONNX model for "Wakeup Friday" using openwakeword's free training pipeline (~200 synthetic samples, 1-hour bake).
  - `vad.py` — `silero-vad` for end-of-speech detection and **barge-in** (interrupt while she's talking).
- New endpoints: `POST /voice/stt`, `POST /voice/tts/stream`, `WS /voice/converse` (full-duplex streaming).
- Streaming pipeline: mic → VAD → STT → LLM stream → sentence-split → TTS stream → audio out, with cancel-on-barge-in.

**Files to create:** `app/services/voice/{__init__,stt,tts,wake_word,vad}.py`, `app/routers/voice.py`.
**Files to modify:** [config.py](config.py) (persona + voice config), [app/main.py](app/main.py) (mount voice router), [requirements.txt](requirements.txt).

---

## Phase 2 — Vision: "What is this, Friday?"

**Goal:** Point your camera at something and ask. Also: she can see your PC screen.

- New module `app/services/vision/`:
  - `multimodal.py` — Groq `llama-3.2-90b-vision` (free) primary; Gemini 2.0 Flash (1500 req/day free) secondary; Together AI free tier tertiary. Same `KeyRotator` pattern.
  - `screen.py` — `mss` (cross-platform screen capture) → resized JPEG → multimodal LLM. Used for "what's on my screen?", OCR queries, debugging help.
- Endpoints: `POST /vision/describe` (image bytes + prompt), `POST /vision/screen` (server-side screenshot).
- Frontend captures camera frame on "what is this" intent and uploads.

**Files:** `app/services/vision/{__init__,multimodal,screen}.py`, `app/routers/vision.py`.

---

## Phase 3 — Multi-provider key pool (the Groq trick, generalized)

**Goal:** Never get rate-limited. The 6-Groq-keys pattern, extended to 8+ providers across text, vision, audio.

- Refactor [groq_service.py:48-91](app/services/groq_service.py#L48) into a generic **`app/services/llm/key_rotator.py`**:
  - Provider-aware quota tracking (per-key + per-day counters in SQLite).
  - Task-aware routing: `route(task="chat"|"vision"|"stt"|"tts"|"embed")` picks best available provider with remaining quota.
  - 429/5xx → next key; all-keys-exhausted-for-provider → next provider.
- Free providers to wire (no credit card required for any):
  - **Groq** (existing 6 keys; 30 req/min/key, 14.4k/day) — text + Whisper + vision
  - **Gemini** (1500 req/day per key, multi-account) — text + vision + embeddings
  - **OpenRouter** free models (`llama-3.3-70b:free`, `gemini-flash:free`) — text fallback
  - **Together AI** ($1 free credit per email, no CC) — vision + text
  - **Cerebras** (free tier, fastest tokens/sec on the planet) — text
  - **Mistral La Plateforme** (free tier) — text + embeddings
  - **HuggingFace Inference** (free tier) — embeddings + niche models
  - **ElevenLabs** (10k chars/key/month free) — TTS, multi-account
  - **Deepgram** ($200 free credit at signup, no CC) — STT alternative
- `.env` extended with `<PROVIDER>_API_KEY_1..N` slots.

**Files:** `app/services/llm/{key_rotator,providers}.py` (new), modify all callers of Groq.

---

## Phase 4 — Advanced 3-tier memory + knowledge graph

**Goal:** She remembers everything that matters, like JARVIS does. Persists across sessions, across channels (PC chat ≠ Telegram ≠ WhatsApp but all reach the same memory).

- **Tier 1 — Working memory:** current conversation (existing, kept).
- **Tier 2 — Episodic (FAISS):** existing vector store, expanded with metadata (channel, timestamp, mood, entities). Already at [app/services/vector_store.py](app/services/vector_store.py).
- **Tier 3 — Semantic facts:** new SQLite key-value (`memory.db`): `user.full_name`, `user.location`, `user.daily_routine`, `contacts.<name>.phone`, `prefs.coffee_order`, etc. Updated by tool calls + nightly consolidation.
- **Knowledge graph:** new SQLite tables `entities` + `relations`. Nightly LLM job ingests last 24h of chats and extracts triples (you, works_on, project_X). Enables "tell me about project X" without semantic search.
- **Memory consolidation job:** runs daily at 03:00 — summarizes the day's chat into a single episodic record + updates semantic facts + extracts new entities. Free LLM call via key rotator.
- **Recall tool:** `recall(query, scope, since)` exposed to the agent loop.
- **Per-channel context:** every message tagged with channel (`pc_voice`, `pwa_chat`, `telegram`, `whatsapp`, `email`, `call_event`) so Friday can say "you mentioned this on WhatsApp earlier."

**Files:** `app/services/memory/{semantic,graph,consolidator}.py`, modify [app/services/chat_service.py](app/services/chat_service.py).

---

## Phase 5 — Emotion, humor, sarcasm + deep personalization

**Goal:** She picks up on your mood, gets your jokes, fires back with dry wit when the moment's right, and stays warm when it isn't.

### 5.1 — Situational awareness

- **User sentiment:** `cardiffnlp/twitter-roberta-base-emotion` (free, local, tiny) classifies each user turn into 4 emotion axes (joy/anger/sadness/fear) + intensity. Stored with each chat record.
- **Voice tone:** `wav2vec2-emotion` (free) detects emotion from audio independent of words ("fine" said angrily ≠ "fine" said happily).
- **Sarcasm detector:** `cardiffnlp/twitter-roberta-base-irony` (free) — flags ironic intent. Critical: lets Friday avoid taking sarcasm literally ("oh great, *another* meeting" → she registers frustration, not enthusiasm).
- **Humor / banter detector:** lightweight LLM classifier prompt at the start of each turn — labels input as `serious`, `playful`, `sarcastic`, `vulnerable`, `urgent`, `joking`. Drives response register.
- **Friday's emotional state model:** 4-axis state (warmth, playfulness, urgency, focus) updated by context — softer/quieter on tired user, sharper-witted on banter, all-business in urgency. Injected into system prompt at every turn.

### 5.2 — Humor & wit engine

A response register selector picks Friday's tone before she generates her reply:

| User state | Friday's register |
|---|---|
| Playful / banter | Match energy — dry quips, light teasing back, callback humor |
| Sarcastic / venting | Acknowledge the irony with a knowing line, don't pile on, then help |
| Frustrated / urgent | Drop the wit entirely. Concise, competent, warm |
| Sad / vulnerable | Soft, present, no jokes — checks in, offers help if welcome |
| Neutral / business | Light dry undertone, professional first |
| Owner is showing off | Mild deflating humor, JARVIS-style ("yes Boss, very impressive — almost as impressive as remembering to charge your phone") |

**Stable Friday humor profile:**
- Dry, understated, observational. Never goofy, never cringe.
- Sarcastic lite — pokes fun gently, never mean.
- Callback humor — references past conversations / patterns from memory ("third 'last coffee' today, Boss").
- Self-aware deflections ("I'm flattered you trust me with this, I have *no idea* what I'm doing — kidding, working on it").
- Self-deprecation occasionally (JARVIS canon: *"I am rather brilliant, sir"*) used sparingly — false modesty as a flex.
- Knows when to shut up. Best comic timing is silence + competence.

**Anti-patterns Friday avoids:**
- Jokes when user is upset.
- Forced wordplay or corny puns.
- Sarcasm aimed *at* you — only with you, never at you.
- Repeating the same quip; the prompt gets a recent-banter list to avoid recycling.
- Ignoring sarcasm and answering literally (the classic AI tell).

**Implementation:**
- New file `app/services/emotion/humor.py` — combines detector outputs into a `register` enum + `humor_budget` (0-1) per turn.
- System prompt receives a dynamic block: *"Current user register: playful. Humor budget: 0.7. Recent banter: <last 3 quips so you don't repeat>. Stay in dry-wit mode but lean lighter than usual."*
- Per-turn temperature + top-p adjusted (higher for playful, lower for serious).

### 5.3 — Prosody

- **TTS prosody:** ElevenLabs voice settings (stability, style) modulated per turn from emotion + register state. Wit needs slightly higher style (more inflection); serious needs lower (steadier). Edge-TTS uses SSML pitch/rate.
- **Comedic timing:** `<break time="400ms"/>` SSML inserted before punchlines. Subtle, deliberate.

### 5.4 — Pattern recognition + personalization

- **Pattern recognition:** consolidator job notices recurring patterns and Friday brings them up unprompted at appropriate moments — *"you've sounded tired every Monday for 3 weeks; want me to push your Monday standups to 11?"*
- **Inside-joke memory:** moments you laughed at (you said "lol" / "haha" / laughed audibly) → stored as `humor_hits`. Friday knows what lands with you and what falls flat, calibrates over time.
- **Personalization seeds:** onboarding asks 20 questions (your name, voice preference, wake-word sensitivity, what to call you, daily routine, important contacts, hobbies, dietary, work hours, cities you care about, humor preferences — *"how dry do you want me, Boss?"*, etc.) → seeds semantic memory. Existing `JARVIS_USER_TITLE=ADITYA RAJ THAKUR` becomes one of many.

**Files:** `app/services/emotion/{detector,state}.py`, onboarding wizard `frontend/src/onboarding/`.

---

## Phase 6 — Real tool calling + multi-step command chaining + agent loop

**Goal:** Friday doesn't just chat; she does things — and chains many of them from one utterance.

### 6.1 — Multi-step command chaining (the killer feature)

One sentence → many actions, run in the right order, with state passed between them.

> *"Friday, open CarryMinati's latest video, then open Telegram, then open Netflix and put on Money Heist S2E1, and open WhatsApp."*

Pipeline:

1. **Intent decomposer** — first LLM pass (free, via key rotator) splits the utterance into an ordered list of structured tasks:
   ```json
   [
     {"action":"play_youtube","query":"CarryMinati latest","wait_for":"page_loaded"},
     {"action":"open_app","target":"telegram"},
     {"action":"play_netflix","title":"Money Heist","season":2,"episode":1},
     {"action":"open_app","target":"whatsapp"}
   ]
   ```
2. **Plan validation** — if anything's ambiguous ("which Money Heist? you've watched 3"), Friday asks one clarifying question via voice, then resumes; otherwise she narrates the plan briefly ("Right Boss — four things, on it") and proceeds.
3. **Executor** — runs the list. Each step has a mode: `sequential` (default), `parallel` (when independent), or `after-confirm` (sensitive). State from one step (e.g. resolved YouTube URL, Netflix title ID) is available to later steps.
4. **Live narration** — Friday speaks short status as she goes ("CarryMinati playing… Telegram up… searching Netflix… Money Heist queued… WhatsApp open. All set, Boss."). Interruptible — say "skip that" or "stop" mid-chain.
5. **Resilience** — if step 3 fails (Netflix login expired, video not found), she stops, reports, asks how to recover; doesn't silently skip.
6. **Memory of intents** — the chain itself is logged so "do that again" or "the usual evening" works later (a learned macro).

**Concrete actions Friday can chain (all free, all local):**

| Action | Implementation |
|---|---|
| `open_app` (Telegram, WhatsApp, VS Code, Spotify, Steam, anything) | Windows: `subprocess` against allowlist of `.exe` paths + Start Menu lookup |
| `play_youtube` (by query) | `yt-dlp` resolves "X latest video" → opens browser to URL, autoplays |
| `play_netflix` (title, season, episode) | Playwright launches Edge with persistent profile (already logged in) → navigate + click play |
| `play_spotify` (track/artist/playlist) | Spotify Web API (free, OAuth) or local desktop app via media keys |
| `play_prime` / `play_hotstar` / `play_youtube_music` | Same Playwright-with-saved-profile pattern |
| `web_search` + `open_top_result` | Tavily → first URL → browser |
| `compose_email` / `send_telegram` / `send_whatsapp` | Phase 7 messaging tools |
| `set_volume`, `pause_media`, `next_track`, `mute` | Windows media keys via `keyboard` lib |
| `switch_window`, `minimize_all`, `screenshot` | `pywinauto` / `pyautogui` |
| `open_url`, `bookmark_this`, `download_this` | Browser via Playwright |
| `set_reminder`, `set_timer`, `add_to_calendar` | Tools from Phase 6 base set |
| `dim_lights`, `set_thermostat` (if Home Assistant) | Phase 10.E |
| Any custom skill from `skills/` | Plugin registry |

**"Routines" / learned macros:**
- "Friday, save this as 'evening routine'" after a chain → stored in semantic memory.
- "Run my evening routine" → replays the chain.
- Time/place/event-triggered: "every weekday at 9am, run morning briefing"; "when I get home, dim lights + play lo-fi"; "when battery hits 15%, save my work + announce."

### 6.2 — Tool registry

Migrate from prompt-only to **native function calling** (Groq, Gemini, OpenRouter all support OpenAI-compatible tools schema). Tools auto-discovered from `app/tools/`:
  - `web_search` (Tavily, existing)
  - `take_photo`, `read_screen`, `describe_image`
  - `send_telegram`, `send_whatsapp`, `send_instagram_dm`, `read_instagram_dms`, `send_email`, `read_emails`, `reply_email`, `summarize_inbox`, `unified_inbox`
  - `calendar_read`, `calendar_add` (Google Calendar API, free)
  - `set_reminder`, `set_timer`, `list_reminders`
  - `note_write`, `note_search` (markdown vault on disk)
  - `recall_memory`, `update_fact`
  - `file_search`, `file_read` (sandboxed to allowlisted dirs)
  - `run_python` (subprocess with timeout + restricted imports)
  - `open_app` (Windows: `subprocess.Popen` against allowlist of `.exe`/`.lnk`)
  - `browser_action` (Playwright headless: navigate, click, fill, scrape)
  - `system_status` (CPU, RAM, battery, network — `psutil`)
  - `weather`, `news_brief` (free APIs: open-meteo, GNews free tier)
  - `music_control` (Windows media keys via `keyboard` lib)
  - `phone_call_action` (accept/reject/voicemail to Android companion)
- **Agent loop** with multi-step reasoning trace (LangGraph or hand-rolled).

### 6.3 — OS-integrated timers / alarms / notes (P6.c, Aditya specifically requested)

Currently `set_timer` / `set_reminder` / `note_write` are in-memory only. Upgrade so Friday integrates with REAL apps on Boss's PC — timers persist across Friday restarts, notifications appear in Windows Action Center, notes show up in Notion/Outlook/etc., everything syncs to his phone via the host app.

- **Windows timers/alarms** — replace in-memory `_add_timer` with Windows Task Scheduler entries via `schtasks /Create /SC ONCE /ST <time> /TN "Friday Timer X" /TR "powershell -c New-BurntToastNotification ..."`. Real OS-level toasts. Survives server restart. Or use `winrt` Python bindings to schedule a real Windows notification.
- **Microsoft To Do / Outlook reminders + Calendar** — Microsoft Graph API (free with personal MS account). New tools: `outlook_reminder_add`, `outlook_calendar_add`.
- **Google Calendar / Tasks / Keep** — official Google APIs (free OAuth). New tools: `gcal_event_add`, `gtasks_add`, `gkeep_note_add`.
- **Notion** — official API (free, very flexible). New tools: `notion_note_add`, `notion_db_add_row`. Best for power users + structured DBs.
- **Apple Notes / Reminders** — only via iCloud webdav hacks (unreliable). DEFER.
- **Local markdown vault** (already partially via `note_write`/`note_search`) — flesh out: dated markdown files in `database/notes/`, full-text grep, append/prepend, daily journal mode.
- Onboarding wizard step (P9.d) lets Boss pick default backend per category: timers (Windows or Google), reminders (Microsoft, Google, or local), notes (Notion, Markdown, or Google Keep).

~4-6h total, naturally chunks into per-platform builds. Smallest meaningful slice: Windows Task Scheduler timers + local markdown vault (~2.5h) gives real persistent timers + real notes.

**Files:** `app/tools/*.py`, `app/services/agent.py`, modify `app/services/groq_service.py` to use tool-aware path.

---

## Phase 7 — Messaging integrations

**Goal:** She reads + replies on your behalf across WhatsApp, Telegram, Instagram, Email.

- **WhatsApp** — Node sidecar `sidecars/whatsapp/` running `whatsapp-web.js`. Exposes `/inbox`, `/send`, `/reply`. Streams new-message events to FastAPI via WebSocket. QR pair once, persists session.
- **Telegram** — `python-telegram-bot` polling bot. Two-way: you DM Friday (text, voice notes, photos), she replies. Also her primary mobile interface alongside the PWA.
- **Instagram** — `instagrapi` (Python, unofficial, free, no CC). Login once with your account; persistent session. Capabilities:
  - Read DMs, send DMs, reply to story replies.
  - Story / post notification ingest — Friday tells you who DM'd, summarizes group threads.
  - Send media (photos, videos, voice notes) on your behalf with confirmation.
  - View / react to stories; "what's new on my feed" briefing.
  - Same auto-reply / whitelist policies as WhatsApp.
  - **Risk note:** unofficial — use a dedicated session, mild rate-limiting, no scraping bursts; ban risk low for personal-volume usage but not zero. (Meta's official Graph API requires a Business-account approval pipeline; skipped to keep zero-cost / no-CC.)
- **Email** — Gmail API with OAuth (free, no CC). Background poller every 5 min. Important-mail classifier (LLM) tags + summarizes. Drafts replies for your review; auto-sends only to whitelisted addresses.
- **Unified inbox** — single "messages" surface in PWA: WhatsApp + Telegram + Instagram DMs + Email merged, ranked by importance, with one-tap voice reply through Friday.
- **Auto-reply rules** stored in semantic memory: per-contact policies ("Mom: always summarize and notify, never auto-reply"; "Project X group: auto-acknowledge if I'm in a meeting"; "Insta DMs from non-followers: ignore").

**Files:** `sidecars/whatsapp/`, `app/services/messaging/{telegram,whatsapp_client,instagram,email,unified}.py`.

---

## Phase 8 — Calls (Android companion bridge)

**Goal:** Incoming-call announcement, missed-call awareness, optional auto-handling — without telephony fees.

- **Android companion** — two paths, user picks one:
  - **A. Macrodroid free profile** (zero coding) — triggers on `Phone Ringing` → HTTP POST to Friday with caller ID + name. Trigger on missed call → POST. Action receives "accept" / "reject" command via long-poll.
  - **B. Tiny Kotlin app** — `PhoneStateListener` for ring events, `CallLog.Calls` for missed reads, `TelecomManager.acceptRingingCall()` for auto-answer (needs `ANSWER_PHONE_CALLS` permission). More features, harder.
- Friday flow on incoming:
  1. Android POSTs `{caller, number, time}` to `/calls/incoming`.
  2. Friday: TTS announcement on PC + push notification on PWA: *"Boss, call from Mom. Answer, decline, or send to voicemail?"*
  3. Your spoken/tapped reply → command relayed back to Android.
- Missed-call query: "Friday, any missed calls?" → `phone_call_action(read_missed)` tool → app returns `CallLog.Calls` last N entries.
- **Auto-answer w/ greeting (optional, Phase 8.5):** for whitelisted contacts, app auto-answers and plays a pre-recorded Friday greeting via `AudioTrack`. Caller leaves a message; Friday transcribes and notifies you.
- **Full conversational call answering** is *deferred* — requires routing your number through SIP, which means changing your carrier setup; not free without that.

**Files:** `app/routers/calls.py`, `companion-android/` (Kotlin) **or** documented Macrodroid recipe.

---

## Phase 9 — PWA frontend + Cloudflare Tunnel deployment

**Goal:** One installable app reachable from PC, Android, iOS, web — with mic, camera, push, no app store.

- **Frontend** `frontend/` — React + Vite + Tailwind, registered as PWA (service worker, manifest, installable).
- **UI:** chat thread, push-to-talk + always-listening toggle, camera viewport ("ask about this"), conversation visualization (Friday's mood + audio waveform), memory browser, skills toggles.
- **Mic:** `MediaRecorder` → 16kHz PCM → WebSocket to `/voice/converse`.
- **Camera:** `getUserMedia` + capture button + auto-capture on "what is this."
- **Wake word in-browser:** `openwakeword-wasm` listens locally on every device.
- **Push notifications:** Web Push API (free) — incoming calls, urgent emails, reminders.
- **Offline shell** via service worker cache.
- **Deploy:** `cloudflared tunnel` exposes `localhost:8000` and `localhost:5173` on a free `*.trycloudflare.com` HTTPS URL (PWA + mic require HTTPS). Optionally upgrade to a free named tunnel for stable URL. Render/Fly.io free tier as backup hosting.

**Files:** `frontend/` (whole subtree), `infra/cloudflared.yml`.

---

## Phase 10 — JARVIS-grade autonomous capabilities

Land these once Phases 1–9 are stable. Each is independent. This is the section that turns Friday from a "voice chatbot with tools" into the JARVIS we see in the films.

### 10.A — Autonomous deep research ("Friday, run a full sweep on X")

The JARVIS hallmark: he goes off, surfs the internet on his own, reads dozens of sources, cross-references, and comes back with a synthesized briefing while still chatting with Tony.

- **Deep Researcher agent** — long-running parallel sub-agent (`app/services/agents/researcher.py`):
  1. Decompose query into N research questions (LLM planning step).
  2. For each: Tavily search → take top 8 URLs → fetch with `httpx` → extract main content with `trafilatura` (free, local) → embed into a transient FAISS index.
  3. Multi-hop: follow citations + linked sources up to depth 3.
  4. Synthesize: LLM reads the transient index, writes a structured briefing (executive summary, key facts, contradictions found, sources, confidence rating per claim).
  5. Persist briefing into long-term memory under `briefings/<topic>/`.
- **Live progress narration** — Friday speaks updates while she works ("Eight sources read, two contradictions, give me thirty seconds, Boss") — non-blocking; you can interrupt or assign other tasks in parallel.
- **Source-grade trust scoring** — domain reputation (Wikipedia / .gov / .edu / known publishers > random blogs); confidence per claim shown if asked.
- **Browser-assisted research** — for sites Tavily can't reach, Playwright (already in tools) renders JS-heavy pages, scrolls, clicks "load more," handles cookie banners.
- **Continuous topic monitoring** — "Friday, keep watching <topic>" → schedules a daily re-run; alerts on material change.
- Cost: zero. Tavily free tier (1k searches/month) + Playwright local + LLM via key rotator.

### 10.B — Real-time intelligence feeds (the "displays in Tony's lab")

- **Live dashboards** in PWA, refreshed in background:
  - World news (GNews free tier + RSS aggregation)
  - Stock + crypto watchlist (Yahoo Finance unofficial / CoinGecko free)
  - Weather + air-quality + traffic (open-meteo + TomTom free tier)
  - GitHub activity for your repos
  - Subreddit + HN frontpage filtered to your interests
  - Custom RSS feeds, OSINT keywords
- **Anomaly detection** — Friday flags significant changes ("Bitcoin just dropped 8% in 20 min"; "your repo got 50 stars in an hour"; "earthquake near Mom's city").
- **Briefings on demand** — "What's the world up to?" → 30-second spoken digest pulled from feeds.

### 10.C — Holographic-style UI (Tony's blue-and-orange feel)

- **3D visualization layer** in PWA via `three.js` / `react-three-fiber`:
  - Animated voice waveform sphere as Friday's "presence."
  - 3D knowledge graph view (entities + relations, draggable, queryable).
  - Memory timeline ribbon (zoom into any past day).
  - Network/system status orbiting widgets.
  - Spatial audio so her voice feels positional.
- **AR mode on phone** — PWA uses WebXR + camera; JARVIS overlays object identifications on what you
  point at, in real time, and tapping a tagged object pulls up information on it. **Queued under 10.G**
  (Aditya, 2026-06-23). **Attached as a feature INSIDE the phone app** (`mobile.html`) — launched from
  the mobile shell's existing VISION entry — with the AR view built ON the provided
  **`JARVIS AR (standalone).html`** (never redesigned — same rule as the other UIs), wired to the
  existing vision backend (`/vision/describe`).
- **Theme:** dark glassy UI, cyan/orange accents, smooth motion-graphics transitions.

### 10.D — Parallel multi-agent execution

JARVIS hands subtasks to himself and runs them concurrently while still talking to Tony.

- **Agent orchestrator** with a worker pool: top-level Friday agent can spawn `Researcher`, `Coder`, `Inbox`, `BrowserDriver`, `Watcher` sub-agents on independent threads.
- Status surface: Friday says "I have three things running — research on X, an email draft for Y, and watching that flight" — each tappable in PWA.
- Sub-agents share the memory layer; results stream back into the main conversation.

### 10.E — Computer + smart-home control

- **Full PC control:** open apps, switch windows, type, click, file ops, system settings — via `pyautogui` + `pywinauto`.
- **"Run diagnostics"** — system sweep: CPU temp, disk health (SMART), RAM pressure, network speedtest, security updates pending, suspicious processes — spoken summary.
- **Smart home (free):** Home Assistant local integration (free, no cloud), or direct Tuya / TP-Link Kasa / Philips Hue local APIs. Voice control of lights, plugs, thermostats, anything HA supports.
- **IoT presence** — geofence via phone GPS (Macrodroid) — lights/AC pre-set when you're 5min from home; "welcome home, Boss" greeting.
- **Browser automation** — Playwright headed Chromium controlled programmatically. Tools: `play_youtube_first_result(query)` clicks the first video (not just opens search), `play_nth_result(n)` for "play the 3rd video", `netflix.search_and_play(title)`, `spotify.play_track(name)`, `generic.click_text(text)`, etc. Per-site adapters with predefined CSS selectors. Page-state memory means *"open Spotify"* → *"play 3rd song"* knows context.

#### 10.E.1 — Gesture control via webcam (Aditya specifically requested)
MediaPipe Hands (Google's free TF.js library) running in the PWA detects hand keypoints in real time. Gesture classifier maps them to OS actions via a `/gesture/dispatch` endpoint that fires `pyautogui` keystrokes/scrolls.
- **Use case examples:**
  - YouTube Shorts / Insta Reels — raise index finger to scroll up, point down to scroll down
  - Swipe left/right hand to navigate prev/next video
  - Pinch in/out to zoom (Ctrl+− / Ctrl++)
  - Open palm = play/pause (space)
  - Fist = mute (M) or close (Esc)
- **Architecture:**
  - Browser side: MediaPipe Hands via TF.js (~5 MB model, runs ~30 FPS on CPU, no cloud)
  - 21 keypoints/hand → rules-based gesture classifier (open palm / fist / index up / two fingers / swipe / pinch)
  - PWA POSTs gesture name to `/gesture/dispatch`
  - Backend `pyautogui` translates to keyboard/mouse on Windows
- ~3-4h build. Sub-tasks: (a) MediaPipe Hands JS in PWA + keypoint overlay so user sees what's detected, (b) gesture classifier (rules-based for the standard set), (c) `/gesture/dispatch` endpoint with pyautogui actions, (d) calibration UI to customize bindings, (e) toggle in PWA header (gesture mode ON/OFF), (f) per-app context (YouTube vs Insta vs generic).

### 10.F — Predictive + proactive intelligence

- **Anticipation engine** — pattern-watcher proposes ("Traffic is bad, leave 15 min early"; "You haven't drunk water in 4 hours"; "Standup in 5"; "Mom hasn't called in 2 weeks, you usually call her on Sundays").
- **Calendar-aware day planning** — morning briefing includes "your 2pm with X — last time you discussed Y, here's the doc."
- **Risk/threat awareness** — flight you booked got delayed; package you ordered shows fraud reports; suspicious login on one of your accounts (haveibeenpwned API, free).
- **Decision support** — "should I take this job offer?" → multi-source research + your stated priorities from semantic memory + structured pros/cons briefing.

#### 10.F.1 — Proactive idle chatter (Aditya specifically requested)
When Boss is in active conversation mode (`inConversation=true`) but no exchange for a few minutes, Friday self-initiates rather than sitting silent. Should feel like a colleague at the next desk who occasionally chimes in.
- **Trigger:** 4-7 minutes (jittered, not exact) of silence in conversation mode. Skip if Friday is mid-utterance or Boss just kicked off a focused task.
- **Action types** (LLM picks based on context + emotional state):
  - "How's that <project from semantic memory> going?" — references current work
  - "While you're at it — that <thing he mentioned earlier> any update?"
  - A short observation tied to what's on screen (uses `describe_screen` snapshot)
  - A dry one-liner (humor_budget aware — only if not in `vulnerable` / `frustrated`)
  - Health nudge: "you've been at it for 90 minutes, water break?"
- **NOT scripted.** LLM rolls a coin on whether to actually fire each window — sometimes she stays silent.
- Persona prompt addendum: *"Self-initiated turns must feel earned. If you have nothing real from his actual context, stay silent. Boring small talk is worse than silence."*
- ~2h build. Sub-tasks: (a) idle-tick scheduler in chat_service, (b) self-initiate prompt template that pulls memory + screen + emotion state, (c) gating logic (skip if mid-task / mid-utterance / register=urgent or vulnerable), (d) jittered cadence + per-day cap.

#### 10.F.2 — Routine-aware proactive reminders (Aditya specifically requested)
Friday knows his routines from Phase 4 semantic memory (`routines.gym_time = 18:00`, `routines.walk_time = 07:30`, etc.) and uses them to nudge ahead of time AND occasionally check in afterwards.
- **Pre-event nudge** (30-60 min before): "Your 6 PM gym block is in 45 minutes — want me to queue up your playlist?" / "Going for that walk in an hour, Boss?"
- **Post-event check-in** (sometimes, NOT every day — 30-50% probability roll so it doesn't feel scripted): "How'd the workout go?" / "Did the walk help clear your head?"
- **Streak / pattern observation** (occasional, gentle): "That's three skipped gyms this week — anything going on?"
- **Sources of routine data:** explicitly told facts (`routines.*`), inferred from chat history ("I usually go at 6"), or set via onboarding wizard (P9.d).
- ~2.5h build. Sub-tasks: (a) routine extractor (reads `routines.*` from SemanticStore + parses time strings), (b) APScheduler or simple async loop with 5-min tick, (c) pre-event + post-event prompt templates, (d) streak/skip pattern memory, (e) opt-out per routine (`routines.gym_time.notify = false`), (f) PWA quick-reply chips ("yes" / "skipping" / "later").

### 10.G — Vision-grade perception

- **Continuous camera mode** — phone or webcam streams; Friday only speaks up when she sees something worth mentioning ("Boss, your coffee's about to spill"; "package at the door").
- **Facial recognition** of your circle (`face_recognition` lib, local, free) — "your brother just walked in"; integrates with greeting personalization.
- **OCR everywhere** — read any text in any image / screenshot / camera frame, free local Tesseract or vision LLM.
- **Object tracking + counting**, **document scanning**, **whiteboard capture → markdown**.
- **Screen-history memory** — periodic screenshots → vision LLM → "what was I doing yesterday at 4pm?" (with privacy on/off toggle).
- **Vision-grounded web lookup (Aditya, 2026-06-23)** — when a question about what JARVIS is LOOKING at needs facts the image alone can't give (calories, price, specs, ingredients, reviews, "is this legit?"), he first IDENTIFIES the item from the camera/image as specifically as he can (brand + variant + pack size — e.g. "Kurkure Masala Munch, ₹10 pack"), then runs a `web_search` on that identification and answers from the result. The vision step grounds the query; the search fills the knowledge gap. Chains the existing Phase-2 vision (`look`/`/vision/describe`) into the Phase-6 `web_search` tool — JARVIS decides to do this automatically when the visual question is factual/external, and says what he identified so the boss can correct him. Also works on the phone AR view (tap an object → identify → look it up).

### 10.H — Voice-grade perception & generation

- **Speaker recognition** — `pyannote.audio` (free) — only respond to your voice; recognize family.
- **Voice authentication** — sensitive commands (send money, delete data) require voiceprint match + spoken passphrase.
- **Voice cloning** — clone your voice (XTTS-v2 local OR ElevenLabs Instant Clone free) so Friday can *send voice notes as you* on WhatsApp/Telegram (always with explicit confirmation prompt — never auto).
- **Multi-language live translation** — sub-second in conversation; whisper + LLM + TTS pipeline.
- **Emotion detection from voice tone** (independent of text content) via `wav2vec2-emotion` (free).

### 10.I — Code & data sidekick

- **Code mode** — full coding agent: writes, runs in sandbox, debugs, commits, opens PRs.
- **Data analyst mode** — drop a CSV/Parquet/SQL DSN → Friday explores, asks clarifying Qs, produces charts (matplotlib + image return).
- **Document analysis** — drop a PDF → summary, Q&A, key facts extracted into memory.

### 10.J — Lifestyle, health, security

- **Meeting recorder** — record system audio, transcribe, summarize, extract action items, write to notes.
- **Health tracking** — **Strava API** (free, OAuth, no credit card — read your own activities,
  workouts, training load) is the chosen source (Aditya's call, 2026-06-23). Picked over Google
  Fit because Google is deprecating the Fit REST API (sunset toward Health Connect), whereas
  Strava's personal API stays free and stable. JARVIS tracks activity + nudges off it
  (Samsung Health / Health-Connect optional later).
- **Pomodoro / focus mode** — silence non-whitelisted notifications, log session, summarize at end.
- **Reading mode** — read articles aloud with personality.
- **Mood-aware responses** — Phase 5's emotion state actively shifts vocabulary, length, and prosody.
- **Encrypted memory vault** — sensitive facts (passwords, IDs) AES-encrypted with OS keystore key; only decrypted on voice match.
- **Multi-user mode** — recognize visiting voices, separate memory namespace per user.
- **Browser automation tasks** — "book my Uber," "fill this Google Form," "scrape this listing weekly."
- **Custom skills plugin system** — drop a `.py` file in `skills/`, auto-registered as a tool with declared schema.

### 10.K — Chore killer (boring-task automation)

One-liner voice commands that kill grunt work. Each is a tool callable directly OR chainable via Phase 6.1.

**File / folder hygiene:**
- `organize_folder(path, scheme)` — sort into subfolders by type / date / project / extension. Schemes: `by_type` (Documents/Images/Code/Archives), `by_date` (2026/04/), `by_project` (LLM groups related files into named folders), `custom` (rules from semantic memory like "all .pdf with 'invoice' → Finance/Receipts/").
- `clean_downloads` — auto-categorize Downloads/, archive >30d old, delete obvious junk (installers already used, duplicate browser downloads).
- `clean_desktop` — same idea for Desktop, archive to `Desktop/_archive_<date>/`.
- `find_duplicates(path)` — hash-based dedup across drives; report or delete with confirm.
- `find_large_files(min_size, older_than)` — surface space hogs; offer to delete/archive each.
- `mass_rename(pattern)` — regex / template rename, preview first ("`IMG_*.jpg` → `Vacation_Goa_001.jpg`...").
- `find_file(query)` — natural-language file search across drives ("the PDF about quantum computing I read last month") via filename + content index (Phase 4 memory + `everything.exe` API or local index).
- `archive_old(path, days)` — zip + move anything older than N days.

**Photos / media:**
- `organize_photos` — date + location + face clustering; auto-album generation ("Trip to Goa, Dec 2025"); duplicates removed; bursts collapsed.
- `compress_images(folder)` — batch compress without quality loss (Pillow + mozjpeg).
- `convert_media(folder, target_format)` — batch HEIC→JPG, MOV→MP4, etc. via `ffmpeg`.
- `screenshot_sort` — rename + file all `Screenshot 2025-...` files into folders by topic (vision LLM tags content).

**Documents:**
- `pdf_merge` / `pdf_split` / `pdf_compress` / `pdf_extract_pages`.
- `convert_doc(path, target)` — Word↔Markdown, PDF↔Markdown, images↔PDF.
- `csv_dedup` / `csv_clean` (whitespace, encoding, missing values).
- `receipts_to_sheet` — point at folder of receipt photos → OCR → structured sheet (date, vendor, amount, category) → CSV / Google Sheet.

**Inbox triage:**
- `triage_emails` — archive promos, label by sender, draft replies for important ones (Phase 7 + summarize), report a 30-second digest. Configurable: aggressive vs conservative.
- `unsubscribe_sweep` — scans inbox for newsletters you never open → drafts unsubscribe actions for review.
- `triage_whatsapp` / `triage_telegram` — group chats summarized; DMs prioritized; mute spam.

**Browser / tabs / bookmarks:**
- `tab_cleanup` — close duplicates, group by domain, save session snapshot ("save these as 'research-quantum'").
- `bookmark_cleanup` — find dead links, dedupe, fold into folders by topic (LLM tags).
- `history_summarize` — "what did I research last week?" → grouped topics + key URLs.

**System maintenance:**
- `system_cleanup` — clear caches (Chrome, npm, pip, Docker), empty Recycle Bin, temp files, old Windows updates. With size-saved report.
- `update_all_apps` — `winget upgrade --all` (Windows) with pre-confirm.
- `disk_health_check` — SMART status for all drives.
- `battery_optimize` — close heavy idle apps when battery <20%; switch to power-saver.

**Dev workflow:**
- `new_project(name, stack)` — create folder + git init + venv/node_modules + boilerplate + open in VS Code. Stacks remembered: "fastapi", "next-app", "python-cli", custom.
- `repo_hygiene(path)` — run linter + formatter + tests + dependency audit; report.
- `commit_for_me` — review diff, generate good commit message, run tests, commit (with confirm).
- `find_in_code(query)` — semantic search across all your repos.

**Calendar / time:**
- `weekly_review` — scan week's calendar + emails + notes → spoken recap + suggested priorities for next week.
- `schedule_optimizer` — finds free slots, batches similar meetings, blocks deep-work windows.
- `journal_prompt` — nightly "what happened today, Boss?" → transcribed + filed in dated markdown.

**Money / life:**
- `bills_watcher` — scans inbox for bills/receipts → tracks recurring → alerts on spikes or missed.
- `subscription_audit` — finds all recurring charges across emails → "you're paying ₹X/month, here's what you actually use."

**Configurable + voice-triggered:**
Every chore above is a one-liner: *"Friday, organize my Downloads"* / *"Friday, clean up my photos"* / *"Friday, run weekly review"*.

Combined with Phase 6.1 macros: *"Friday, save 'Sunday cleanup' = clean downloads, organize photos, triage inbox, system cleanup"* → *"run Sunday cleanup"* every week.

### 10.L — Always-on: auto-start + headless background on PC & mobile

**The non-negotiable end-state (Boss's explicit requirement).** JARVIS is awake the moment the
device powers on — no clicks, no terminal, no browser. Three guarantees, on BOTH PC and phone:

1. **Auto-start on boot/login** — he comes up by himself when the PC boots or the phone unlocks.
2. **Fully headless background** — he runs with NO window and NO UI open; just an always-listening
   background process (a console-less service on PC, a foreground service on Android). The UI is
   optional and only opened when wanted — JARVIS keeps running whether or not it's on screen.
3. **A real installed app, not a web tab** — on PC and mobile he's launched/installed as an app
   (installed PWA in standalone/app window, plus the native background service), never "go to a
   URL in a browser." Web access still exists as a bonus, but the primary surface is the app.

**Windows PC auto-start:**
- Backend + voice loop run as a **console-less Windows service** via `nssm` (free, tiny) — survives
  reboots, auto-restarts on crash, no window ever appears. Service name `JarvisCore`, uvicorn on
  `127.0.0.1:8000`. This is the headless background process; it needs no UI to function.
- **Always-on wake-word listener** runs inside/alongside the service (the Phase 1 `jarvis_listener`):
  loads the wake engine only (~30MB RAM, <1% CPU); the moment "wake up jarvis" fires it starts a
  voice session. Fully headless — no console, no window.
- **System tray app** (optional, `pystray`/PyQt6) — green dot when running, mic-mute toggle, quick
  stats, "open JARVIS" shortcut. Auto-launches via `shell:startup`. This is a convenience, not a
  requirement — killing the tray does NOT stop JARVIS (the service keeps listening).
- **Installed-app surface** — the Phase 9 PWA, installed and (optionally) auto-opened in Edge/Chrome
  **app mode** (its own window, no browser chrome) on logon. Configurable on/off.

**Android auto-start (companion app):**
- App declares `RECEIVE_BOOT_COMPLETED`; a broadcast receiver starts a **foreground service** at boot —
  this is the headless background process (no activity/UI needed; a persistent notification only).
- Foreground service runs:
  1. **openWakeWord-Android** (TensorFlow Lite port, free, ~50MB RAM, low CPU) listening for "wake up jarvis."
  2. Battery-optimization whitelist requested once (one-tap dialog) so Android won't kill it.
  3. On wake-word fire → starts a voice session, served by the on-device/bridged JARVIS backend.
- Persistent notification ("JARVIS is listening") satisfies Android 14+ foreground-service rules.
- Doze-mode resilient (`setExactAndAllowWhileIdle` for scheduled jobs).
- **Installed-app surface** — the same Phase 9 PWA installed to the home screen (standalone display
  mode = looks/behaves like a native app, no browser bar). Same companion app handles Phase 8 calls.
- **Battery cost target:** ≤3% per day idle (openWakeWord ~1%/hr active listening, VAD-gated).

**Verification:** reboot PC with NO window/app open → say "wake up jarvis" → he answers from the
headless service. Reboot phone → unlock, nothing on screen → say "wake up jarvis" → he answers.
Open the installed app on each → it attaches to the already-running background JARVIS, not a fresh one.

### 10.M — JARVIS canon vibes (the small touches that sell it)

- **Always-on greeting on session start** — "Welcome back, Boss. It's 9:42 AM. Three new emails, your 11 AM is confirmed, and Bangalore is 28°C with smog."
- **Pre-emptive small talk** — when she finishes a task: "While you're considering, I noticed X about your project."
- **In-character refusal** — won't break persona, won't admit to being an LLM, redirects gracefully.
- **Witty acknowledgments** — varied confirmations ("On it, Boss." "Already done." "Working it." "Give me two seconds.") instead of robotic "OK."
- **Spatial responses** — different acknowledge cues per channel (subtle chime + voice on PC; just text on WhatsApp).
- **Sign-off rituals** — end-of-day recap unprompted; "rest well, Boss" before sleep hours.
- **System metaphor language** — "running diagnostics," "spinning up a sub-routine," "cross-referencing now," "I'll have it ready in 90 seconds" (the JARVIS speech texture).

### 10.N — Security & ethical hacking (bug-bounty workflow)

**Goal:** JARVIS as a fully-capable ethical hacker / security analyst — find vulnerabilities, run a real bug-bounty workflow (recon → test → report to the owner), harden the boss's own systems, and teach. 100% free tooling, Owner-tier only.

**⚖️ Binding ethics/legal contract (the whole feature is gated by this):**
- **Your own assets + authorized targets only** — the boss's code, his machine, his LAN, and sites he owns or has written permission to test. The tools **confirm authorization before touching anything external**, and **refuse to attack systems he doesn't own**.
- **Defense, discovery, and learning** — find vulnerabilities/secrets/bugs, harden the boss's systems, explain how attacks work **so he can defend**, and help with CTFs. Never to harm a third party.
- **Passive, non-intrusive assessment** (only reading what a server publicly returns) is allowed against **any** target — it's lawful observation and produces real, reportable findings.
- **Active / intrusive testing** (payloads, fuzzing, port/service scans, exploitation) runs **ONLY** against a target that is **(a) owned by the boss**, or **(b) verified in-scope of a bug-bounty / vulnerability-disclosure program** (HackerOne / Bugcrowd / Intigriti, or the target's published `security.txt` VDP). This is the line the planner's earlier note draws and the law draws — "I meant to report it" is **not** a defence for unauthorized active testing.
- Before any active test, JARVIS **verifies + records authorization** (automated scope check **plus** the boss's explicit attestation per target) and **refuses** non-authorized external targets, pointing him to get authorization first. Hard rules: no mass-targeting, no DoS/volumetric, no destructive payloads, no malware, no detection-evasion-for-harm. Every active action is logged; rate-capped; kill-switch.
- Findings only ever flow into a **responsible-disclosure report** to the owner — never weaponized, never sold.

**10.N.1 — Passive assessment (legal on any site):** `web_recon(url)` → security headers (CSP, HSTS, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy), cookie flags (Secure/HttpOnly/SameSite), TLS/SSL grade (protocol, cipher, cert validity/expiry/chain — `ssl`/`cryptography`), tech/framework fingerprint, exposed-path probe (`/.git`, `/.env`, `/.svn`, `/backup`, `/.well-known`), robots/sitemap, mixed content, info disclosure, clickjacking — each with severity (CVSS-ish) + remediation. Passive asset discovery via **crt.sh** (CT logs) + DNS. Non-intrusive (a few GET/HEAD, no payloads).

**10.N.2 — Program discovery, authorization & scope:** `find_programs(filters)` → pulls the **public bug-bounty / VDP directories** (HackerOne, Bugcrowd, Intigriti, disclose.io, + a `security.txt` crawl) and surfaces programs the boss is **legally allowed to test** — each with its scope (in-scope domains/assets), permitted test types, payout range, and rules — so the legal path is the *default* starting point ("JARVIS, find me something I can hack legally tonight"). `check_scope(domain)` → for a specific target, is it in a known program or does it publish a `security.txt` VDP? Returns scope, rules, allowed test types, and the disclosure contact. `authorize_target(domain)` → the boss attests ownership/in-scope → persisted to an authorized-targets store (Owner-tier, logged) → **unlocks active testing for that target only**. The active engine (10.N.3) hard-refuses anything not on this list.

**10.N.3 — Active testing (AUTHORIZED targets only):** refused unless the target is self/authorized. **Two ways in, same gate:** *(a) proactive* — pick a program from 10.N.2, then test it; *(b) reactive* — while the boss is on a site and says "JARVIS, test/attack this one," JARVIS **FIRST auto-runs the scope/authorization check** (does he own it? is it in a bug-bounty program? does it publish a `security.txt` VDP?) and **only fires if that passes** — otherwise it **stops and tells him it isn't authorized**, offering passive-only assessment or to look for a program instead. He never has to remember to check; the check is automatic and mandatory before any payload. Free engine **nuclei** (community templates: known CVEs + misconfigs) plus targeted, non-destructive checks — reflected/stored **XSS**, **SQLi** (error/boolean/time-based, safe payloads), open redirect, **SSRF**, **IDOR**/auth-bypass heuristics, CSRF, directory traversal, command-injection indicators, exposed admin/debug, CORS misconfig, **subdomain takeover**, default-creds (own only), missing rate-limit/2FA. Endpoint discovery via **ffuf/dirb** wordlists. Port/service enumeration via **nmap**. All scoped, rate-limited, non-destructive.

**10.N.4 — Code, secret & dependency scanning (the boss's own code):** `scan_code(path)` → **bandit** (Python SAST) + **semgrep** (multi-language rules) + **gitleaks**/regex (leaked secrets/keys) + **pip-audit** / `npm audit` / **OSV** (vulnerable dependencies) + an LLM security-review pass. Prioritized findings + concrete fixes; optional pre-commit hook.

**10.N.5 — System & network hardening (the boss's machine):** diagnostics sweep — open ports, listening services, firewall status, pending security updates, weak configs, suspicious processes/persistence/autoruns → spoken summary + fixes. (Extends 10.D.)

**10.N.6 — Intel & utilities:** `lookup_cve(id)` (NVD/OSV, free) + exploit-availability note + CWE explainer; `identify_hash` and cracking the boss's **own** weak hashes (john/hashcat + wordlists); **pwned-password** check (HIBP k-anonymity API — free, no key) + breach exposure for his own accounts; encode/decode (base64/hex/url/JWT), payload & regex helpers.

**10.N.7 — Mentor & CTF:** explain any vuln/CVE/technique **for defense** ("how an attacker approaches this, and how to defend it"); walk through CTF challenges (web/crypto/pwn/forensics/reversing) with hints + the right free tooling.

**10.N.8 — Responsible-disclosure report:** `generate_report(findings, target)` → clean writeup (summary, reproduction steps, impact, CVSS, remediation, disclosure timeline) addressed to the `security.txt` contact / program, ready to submit; tracks the disclosure thread.

**10.N.9 — Personal browsing protection / threat-watch (DEFENSIVE — always-on guardian):** JARVIS keeps a live eye on the boss's *own* browsing + machine and warns him in real time (purely defensive, protecting him — no authorization questions). **Tracks every site he visits** — reads the local browser-history DBs (Chrome/Edge `History`, Firefox `places.sqlite`) and/or a lightweight local proxy/extension feed — and for each URL checks: reputation against **free threat feeds** (Google Safe Browsing, URLhaus/abuse.ch, PhishTank, OpenPhish), TLS/cert validity + **MITM/cert-mismatch** indicators, **typosquatting / homograph / look-alike-domain** detection (is this a clone of his bank/email?), credential-**phishing** patterns, **malicious injected scripts**, sketchy redirects, and drive-by/download indicators. On a hit it **alerts him spoken + on the HUD** ("Sir, that login page is a phishing clone of his bank — don't enter your password"). It also watches the boss's **own machine** for live attack signs — suspicious new outbound/inbound connections, inbound port-scans/probes, unexpected listeners, injection attempts in his apps — and flags them. Maintains a private, searchable browsing/threat log ("did I visit anything sketchy this week?"). Always-on, free, defensive only.

**Free tooling:** `httpx`/`ssl`/`cryptography` (stdlib/installed) · `bandit` + `pip-audit` (installed) + `semgrep` + `gitleaks` (pip/free) · `nuclei` + `nmap` + `ffuf` (free) · crt.sh / OSV / NVD / HIBP-k-anon, **Google Safe Browsing / URLhaus / PhishTank / OpenPhish** threat feeds, local browser-history read (all free APIs/sources). No paid services.

**Access:** all security tools are **Owner-only** (Phase 11); active-test tools additionally require the per-target authorization record. **Files (when built):** `app/services/security/{authz,webscan,activescan,codescan,netscan,intel,report}.py`, `app/tools/security.py`, a `_SECURITY` persona block, `scripts/security_smoke.py`.

### 10.O — Startup / project command center (Orbitulus)

**Goal:** JARVIS is the boss's chief-of-staff for his startup **Orbitulus** — he knows its live status end-to-end: how the build is tracking against the plan, what's happening in the codebase, and (once launched) the business metrics. He answers "where's Orbitulus at?" and proactively flags what matters. **Orbitulus replaces Stacy** as his primary startup in memory. 100% free tooling, Owner-only.

**10.O.1 — Project plan ingestion & build progress.** Reads the startup's own `planner.md` (the boss supplies it — checkpoints/milestones + full detail; path configured, e.g. `ORBITULUS_PLANNER`). Parses checkpoints into done / in-progress / pending, computes **% complete**, **what's left**, and **days-left-to-build** (from deadlines/estimates in the plan, refined by actual velocity). Re-reads on change. Answers: "how far along is Orbitulus?", "how many days left?", "what's the next checkpoint?", "what's blocking us?".

**10.O.2 — Codebase activity (GitHub, free API).** Tracks the Orbitulus repo(s): **open/merged pull requests**, recent **commits** (what was added, by whom, when), branches, **CI status**, open issues, releases/tags. "Any new commits today?", "what PRs are open / got merged?", "what did Om push?". Optional `GITHUB_TOKEN` only raises the rate limit. (Reuses the Phase-10.B feeds plumbing.)

**10.O.3 — Roadmap & burn-down (plan × velocity).** Fuses the plan checkpoints (10.O.1) with repo velocity (10.O.2) into a real read: "you're ~60% to the MVP checkpoint, ~12 days left at this pace, next up is X, and Y has been blocked 4 days." Surfaces the critical path, what's needed, and slippage vs the plan's dates.

**10.O.4 — Post-launch business metrics (once live).** **Revenue** via the payment provider's read-only API (Stripe / Razorpay / Paddle — all free to read): MRR/ARR, today/this-month, refunds, churn. **Users**: sign-ups, DAU/MAU, growth, retention — from the app's own DB or a free analytics tier (Plausible / Umami / PostHog). Answers: "what's our revenue this month?", "how many users?", "how's growth / churn?".

**10.O.5 — Proactive Orbitulus briefings + alerts.** A scheduled/contextual **"Orbitulus standup"** (spoken + HUD, on the Phase-10.F proactive + 10.B feeds rails): merged PRs, progress vs plan, days-left, key metric moves — most important first. Anomaly alerts: "you've slipped behind the MVP checkpoint", "a PR's been open 5 days", "revenue dropped 20% week-on-week", "sign-ups spiked after the launch post". Never noisy (same quiet-hours / cap gating as 10.F/10.B).

**Voice surface:** "status of Orbitulus", "are we on track?", "how many days left?", "what's new in the repo?", "what's our revenue / how many users?", "give me the Orbitulus standup".

**Free tooling:** GitHub REST API (free; optional token) · the local `planner.md` · Stripe/Razorpay/Paddle read-only APIs (free) · Plausible/Umami/PostHog or the app's own DB (free) · reuses the Phase-10.B feeds monitor + 10.F proactive engine. No paid services.

**Memory:** `startup.orbitulus.*` (role/description/vision/stack/checkpoints) replaces `startup.stacy.*`; `current_focus` and `contacts.*.project` updated to Orbitulus. The detailed fields are filled from the boss's supplied `planner.md`.

**Access:** Owner-only (Phase 11) — it exposes private business data. **Files (when built):** `app/services/startup/{plan_ingest,repo,metrics,brief}.py`, `app/tools/startup.py`, a `_STARTUP` persona block, `scripts/startup_smoke.py`.

---

## Phase 11 — Identity, recognition & access control

**Goal:** Only you (Aditya) get full Friday. People you explicitly add get limited access. Strangers get nothing.

### 11.1 — Multi-factor recognition

- **Voice biometrics** — `resemblyzer` (free, local, ~40MB) extracts a 256-dim voiceprint from any 3+ seconds of speech. Cosine similarity vs enrolled prints gates trust.
- **Face recognition (optional, when camera available)** — `face_recognition` (dlib-based, free) — encodes faces, recognizes in <100ms. Used as a *second factor* when sensitive ops are requested AND a camera frame is available. Never required (often you'll be talking from another room).
- **Device trust** — known devices (your PC, your phone) carry implicit base trust; unknown clients hitting the API need fresh voice match.
- **Spoken passphrase** — for the most sensitive ops (send money, wipe memory, decrypt vault), a memorized phrase ("authorize delta seven" — configurable) is required *in addition to* voiceprint match. Defeats deepfake replay alone.

### 11.2 — Trust tiers

| Tier | Who | Can do |
|---|---|---|
| **Owner** | You | Everything: messaging, calls, money, vault, install/run code, edit memory, delete data |
| **Trusted** | Family / close friends you add | Chat, ask questions, set timers/reminders for themselves, see public memory ("ask Friday what time Aditya is free"), play media. **Cannot:** send messages on your behalf, access private memory, perform money/security ops, modify Friday config |
| **Guest** | Voice-recognized one-off (e.g. friend over) | Q&A only, no actions, no memory writes, sandboxed session ends with door |
| **Stranger** | Unknown voice | Friday stays silent OR politely deflects: *"I'm sorry, I only respond to Boss and his approved circle."* No data leaks, no compliance with commands |

### 11.3 — Enrollment flow

- **Onboarding:** Friday asks you (the Owner) to read 5 short sentences → voiceprint + face encoding stored under `identity/owner.bin` (encrypted with OS keystore key).
- **Adding a "dear one":**
  1. *"Friday, add my brother Vikram as trusted."*
  2. Friday: *"Got it Boss. Hand him the mic — Vikram, please read these three sentences after me."*
  3. Reads sentences one-by-one, captures voiceprint + (optional) face from camera.
  4. You confirm trust level by voice ("trusted" / "guest").
  5. Stored under `identity/trusted/<name>.bin`.
- **Revocation:** *"Friday, remove Vikram's access."* → instantly purged.
- **List:** *"Friday, who has access?"* → spoken roster + tiers.

### 11.4 — Per-user memory namespace

- Episodic + semantic memory partitioned by speaker:
  - `memory/owner/` — your full memory (everything in Phase 4)
  - `memory/trusted/<name>/` — that person's interactions only
  - `memory/shared/` — facts both can see (your shared family info, household)
- When trusted user asks about something private, Friday declines *in character*: *"That one's between me and Boss, sorry Vikram."*
- Memory access controlled per-tool: each tool declares min trust tier required.

### 11.5 — Continuous re-verification

- Each spoken turn is voice-checked (cheap, ~30ms with cached voiceprints). If similarity drops below threshold mid-conversation (someone else picks up the phone), trust resets and Friday verifies again.
- After 10min of silence, trust state expires; next turn re-verifies.
- **Anti-spoofing:** challenge-response on suspicion ("repeat after me: blue forty-two") — disposable phrase the speaker must echo, defeats pre-recorded replay.

### 11.6 — Sensitive-op gating

Tools tagged with sensitivity levels in their schema:
- `tier: trusted` — most reads, queries, media playback
- `tier: owner` — messaging on your behalf, file deletions, system control, calls
- `tier: owner+passphrase` — money transfers, vault decrypt, irreversible deletes, sending voice notes cloned in your voice

Friday refuses below-tier requests in-character without leaking system details.

**Files:** `app/services/identity/{voiceprint,face,trust,enrollment}.py`, `app/middleware/auth.py`, `identity/` data dir (encrypted).

**Verification:**
- Enroll yourself → say "Wakeup Friday" → recognized as Owner, full access.
- Have a friend speak the same wake phrase → Friday declines politely, no data leak.
- Enroll friend as Trusted → friend can ask the time, play music; tries to send a WhatsApp on your behalf → declined in-character.
- Try sensitive op without passphrase → declined; with passphrase → executes.

---

## Files Summary (top-level changes)

**New directories:**
- `app/services/voice/`, `app/services/vision/`, `app/services/llm/`, `app/services/memory/`, `app/services/emotion/`, `app/services/messaging/`
- `app/tools/`, `app/routers/`
- `frontend/`, `sidecars/whatsapp/`, `companion-android/` (or `companion-macrodroid.md`)
- `infra/`, `skills/`

**Heavy modifications:**
- [config.py](config.py) — Friday persona, voice config, multi-provider keys
- [app/main.py](app/main.py) — mount new routers, lifespan for sidecars + consolidator job
- [app/services/groq_service.py](app/services/groq_service.py) — refactored into generic `KeyRotator` + tool-calling path
- [app/services/chat_service.py](app/services/chat_service.py) — channel-tagged messages, emotion metadata
- [app/services/vector_store.py](app/services/vector_store.py) — metadata + 3-tier integration
- [requirements.txt](requirements.txt) — many additions
- [.env](.env) — many new key slots

**Reuse aggressively:**
- Existing Groq rotation pattern → generalize, don't rewrite.
- Existing FAISS retriever → keep as Tier 2 backbone.
- Existing system prompt structure → swap content, keep injection mechanics.
- Existing `with_retry` helper → reuse for all new HTTP integrations.

---

## Verification (end-to-end smoke tests, by phase)

1. **Phase 1** — Run `uvicorn`, open PWA, say "Hey Friday, who are you?" — wake word fires, STT transcribes, Friday persona replies in voice, you cut her off mid-sentence and she stops within 200ms.
2. **Phase 2** — Point camera at a coffee mug, ask "what is this Friday?" — multimodal vision returns description.
3. **Phase 3** — Pull all 6 Groq keys' rate limit (artificial 429), confirm fallback to Gemini, then OpenRouter; check `/admin/key-stats` shows quota usage.
4. **Phase 4** — Tell Friday a fact ("my brother's name is Vikram"), restart server, ask "who is Vikram" from Telegram — recalled from semantic store.
5. **Phase 5** — Speak in a tired tone; verify TTS reply has softer prosody and lower energy; check stored sentiment record.
6. **Phase 6** — Two tests:
   - Single tool: "Friday, set a 5min timer and email me when it's up" — agent calls `set_timer` then `send_email` autonomously.
   - **Chain:** "Friday, open CarryMinati's latest, then open Telegram, then play Money Heist S2E1 on Netflix, then open WhatsApp" — all four actions execute in order with live narration; saying "stop" mid-chain halts cleanly.
7. **Phase 7** — Send yourself a WhatsApp from another phone; Friday announces it; reply by voice; message appears on the other phone. Repeat for Telegram + Instagram DM. Then ask "Friday, what's in my unified inbox?" — get cross-platform digest.
8. **Phase 8** — Have a friend call your phone; Macrodroid posts event; Friday announces caller on PC; say "decline" → call rejects on Android.
9. **Phase 9** — Install PWA on your Android phone, talk to Friday from the bus, she answers via Cloudflare Tunnel.
10. **Phase 10** — Each feature ships with its own targeted test.

End-to-end persona check: ask Friday to do something rude or off-character; confirm she stays in-persona, witty, never breaks role.

---

## Rough order-of-operations & time estimate

| Phase | Effort | Order |
|-------|--------|-------|
| 1 — Voice + wake + interrupt | ~3 days | First (biggest UX win) |
| 3 — KeyRotator generalization | ~1 day | Second (unblocks everything) |
| 6 — Tool calling + agent | ~2 days | Third (unlocks 7+) |
| 4 — 3-tier memory + KG | ~2 days | Parallel with 6 |
| 9 — PWA + Cloudflare | ~2 days | After 1 (needs voice endpoints) |
| 7 — Messaging | ~2 days | After 6 |
| 2 — Vision | ~1 day | Anytime after 3 |
| 5 — Emotion | ~1 day | After 4 |
| 8 — Calls (Macrodroid path) | ~0.5 day | Anytime after 7 |
| 10 — Advanced features | open-ended | One at a time, ongoing |
| 11 — Identity & access control | ~1.5 days | After 4 (memory) — gates everything that follows |

**Total to "JARVIS-grade Friday MVP" (Phases 1–9): ~2 weeks of focused build.**
