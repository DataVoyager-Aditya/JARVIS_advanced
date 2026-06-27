# JARVIS_advanced — Build Status

Single source of truth for progress. Rules: [.claude/RULES.md](.claude/RULES.md).
A feature is only ✅ when it is **complete, final-quality, and smoke-tested** (per the
finality rule — we do not come back to polish it).

Legend: ✅ shipped & verified · 🚧 in progress · ⏸️ deferred (with reason) · ⬜ not started

---

## Phases (PLANNER order of operations)

| # | Phase | State | Verified how |
|---|-------|-------|--------------|
| 1 | JARVIS persona + streaming voice + wake word + interruption | ✅ | **verified live** by Aditya — "wake up jarvis" fires, accurate STT, in-character persona, streaming British voice, multi-turn conversation. Wake=Vosk(grammar), STT=Whisper+preroll, TTS=Edge Ryan prefetch pipeline |
| 3 | Multi-provider KeyRotator | ✅ | per-key rotation, fallback order groq→gemini→openrouter, STT groq→deepgram, SQLite quota persists, `/admin/key-stats` — all tested |
| 6 | Tool calling + agent + command chaining | ✅ | **verified live** by Aditya — barge-in yields + keeps the interrupting words, web search ~2.7s, no fabricated actions, "thank you" no longer re-runs tools, multi-step chains, persistent OS-level alarms ring |
| 4 | 3-tier memory + knowledge graph | ✅ | full smoke test (`scripts/mem_smoke.py`, 20/20) — FAISS vector recall, semantic facts, knowledge-graph triples, **survives restart**, real nightly consolidator distilled 5 facts + 3 triples, runner injects memory into the prompt. **+ `MY_PROFILE.md`** human-editable profile (89 facts seeded) |
| 9 | PWA + Cloudflare Tunnel | ✅ | built from `JARVIS.html` (design preserved), wired to real agent+memory+voice. Server-tested live: `/chat` answers from profile + working memory, tool→panel hints, mic→STT→agent→TTS, manifest/SW/icons 200, offline shell. JS validated. Tunnel launcher ready (`scripts/run_pwa.py`) |
| 7 | Messaging (WhatsApp/Instagram/Email) | ✅ | **smoke 59/59** (`scripts/messaging_smoke.py`) incl. **live Gmail IMAP** + **live Instagram** + **live WhatsApp**. Persistent cross-channel inbox, dedup, importance classifier, per-channel nicknames, group-aware mute, context-reply, delete/unsend, IG likes/posting/stories/follow, unified HUD panel. **Verified live by Aditya** across many sessions — sends to right contact on both apps, context-reply works, group mute holds. Telegram **dropped**; WhatsApp **calling deferred**. ~26 messaging tools |
| 2 | Vision ("what is this, JARVIS?") | ✅ | **smoke 11/11** (`scripts/vision_smoke.py`) incl. **live screen-capture→vision-LLM** + **live OCR**. Live-verified by Claude on this machine: `read_screen` identified VS Code/PLANNER.md, `look` (webcam) identified objects, `/vision/describe` read text exactly. Groq Llama-4 primary (fast) + Gemini 2.5 Flash accuracy fallback. PWA camera viewport wired (device camera → `/vision/describe`) |
| 5 | Emotion / humor / personalization | ✅ | **smoke 15/15** (`scripts/emotion_smoke.py`) — register detection 7/7 (live local model), humor-budget tracking, mood prompt block + recent-banter dedup, **voice-tone fusion** ('i'm fine' said angrily → frustrated), prosody, humor-hits, end-to-end agent adapts tone. Voice SER inference **0.45s** (parallel with STT → ~0 added latency). HUD EMOTION panel wired to live state |
| 8 | Calls (Android companion bridge) | ✅ | **smoke 57/57** (`scripts/calls_smoke.py`) — persistent call log + dedup, live-ring command queue with TTL, spoken announcements + **hands-free "decline"/"answer"** (ring acts as wake), **outbound dialing** (`place_call`), missed-call surfacing, number→relationship naming, Phase-8.5 auto-handle rules, 4 tools + schemas, router mount + token gating, persona. Backend + PWA call card + desktop announce wired. **Companion ships both ways** — Macrodroid recipe (zero-code, testable now) + a real Kotlin app (`companion-android/`). **Live phone verification pending Aditya's one-time companion setup.** Conversational answering (JARVIS talking *on* the call) is a separate build → **PC-as-Bluetooth-handset**, not the blocked Android call-audio APIs |
| 11 | Identity, recognition & access control | ✅ | **smoke 72/72** (`scripts/identity_smoke.py`) incl. **real voiceprint test** (same speaker 0.89 vs different 0.57, split by the 0.75 gate). Voice biometrics (resemblyzer 256-d) + **face** (OpenCV YuNet+SFace 128-d — Windows-native, no dlib) + trust tiers (owner/trusted/guest/stranger) + per-tool gating + spoken passphrase + continuous re-verification. Biometrics **encrypted at rest via Windows DPAPI**. Voice ID runs in parallel with STT (~0 latency); unknown voices deflected; memory owner-only. **+ mandatory face gate at startup** (greets by name), **voice-guided enrolment** ("add Vikram as trusted" → captures face+voice), **"run face recognition again"** to switch the active user, and the **live active-user name on the PC HUD + mobile** (top-left identity panel). **Open mode until you enrol** |
| 10 | JARVIS-grade autonomous capabilities (A–M) | 🚧 | build order set (below); 10.L in progress |

## Phase 10 — recommended build order (set 2026-06-23)

Phase 10 is 13 independent capabilities (10.A–10.M) **plus 10.N security** (Aditya moved it
into scope on 2026-06-23 — no longer spec-only — and placed it **last** in the queue); PLANNER
fixes no internal order, so this is the agreed, dependency-aware sequence. Built **one at a time,
each final-quality + smoke-tested + recorded** before the next. (10.C deferred per the UI constitution.)

| Order | Sub-phase | State | Why here |
|------|-----------|-------|----------|
| 1 | **10.L** Always-on / auto-start (headless service + tray + boot-launch) | ✅ | smoke 62/62; live reboot check pending Aditya |
| 2 | **10.F** Proactive & predictive intelligence (idle chatter, anticipation, routine nudges) | ✅ | smoke 71/71 + live e2e; **adversarial audit (25 agents) found & fixed 13 real bugs**; live spoken-nudge check pending Aditya |
| 3 | **10.B** Real-time intelligence feeds, extended (anomaly alerts, watchlists, briefings) | ✅ | smoke 65/65 + live e2e; **adversarial audit (16 agents) found & fixed 9 real bugs**; live check pending Aditya |
| 4 | **10.A** Autonomous deep research (multi-source sweep → synthesized briefing) | ✅ | smoke 109/109 incl. **live e2e sweep**; **adversarial audit (34 agents) found & fixed 24 real bugs**; live voice check pending Aditya |
| 5 | **10.D** Parallel multi-agent execution (worker pool) | ⬜ | Generalizes the researcher; reuses 10.A |
| 6 | **10.K** Chore killer (~30 file/photo/doc/inbox/system/dev automations) | ⬜ | Daily-useful; mostly local/free |
| 7 | **10.E** Computer + smart-home control + gesture (10.E.1) | ⬜ | PC automation + MediaPipe gesture |
| 8 | **10.I** Code & data sidekick (coding agent, data analyst, PDF Q&A) | ⬜ | — |
| 9 | **10.G** Vision-grade perception (continuous camera, screen-history, **phone AR mode**, **vision-grounded web lookup**) | ⬜ | OCR + face-recognition already shipped (P2/P11); **AR mode** = real-time WebXR camera object-overlay + tap-for-info, built on the provided `JARVIS AR (standalone).html`; **vision-grounded web lookup** = identify the item then web-search to answer (e.g. calories on a Kurkure pack). Both Aditya-queued 2026-06-23 |
| 10 | **10.H** Voice-grade remaining (voice cloning w/ confirm, live translation) | ⬜ | Speaker-ID, voice-auth, voice-emotion already shipped (P11/P5) |
| 11 | **10.J** Lifestyle/health/security extras (meeting recorder, pomodoro, reading mode, vault) | ⬜ | — |
| 12 | **10.M** JARVIS canon vibes (varied acks, session greetings, sign-off rituals) | ⬜ | Flavor pass that ties it together |
| 13 | **10.O** Startup command center (**Orbitulus** status: plan progress, repo PRs/commits, days-left, revenue/users) | ⬜ | Added 2026-06-23. Reuses 10.B feeds + 10.F proactive rails (both ✅), so buildable soon; needs Aditya's Orbitulus `planner.md` + repo. Owner-only |
| 14 | **10.N** Security & ethical-hacking (bug-bounty workflow, hardening, CTF, browsing guardian) | ⬜ | **Last, at Aditya's request (2026-06-23).** Owner-tier gated (P11 ✅); active testing hard-gated to owned/in-scope targets; built to its full authorization contract |
| — | **10.C** Holographic three.js UI | ⏸️ | Deferred — violates the UI constitution (never redesign; build on the provided HTML). **Its AR-mode sub-feature is pulled out and queued under 10.G** (Aditya, 2026-06-23). A 3D HUD redesign won't ship |

**10.N is built strictly to its binding authorization contract** (PLANNER §10.N): passive/read-only
assessment is allowed on any target (lawful observation); **active/intrusive testing runs ONLY
against targets Aditya owns or that are verified in-scope of a bug-bounty/VDP program** — every
active action auto-checks + records authorization first and hard-refuses otherwise. No DoS/
volumetric, no mass-targeting, no destructive payloads, no malware, no detection-evasion-for-harm.
All security tools Owner-tier only; findings flow only into responsible-disclosure reports.

## Phase 10.L — what shipped

**Always-on: JARVIS comes up by himself at logon, fully headless, and stays up.** Free, no admin,
no window. (PLANNER §10.L.)

- **Headless supervisor** (`scripts/jarvis_supervisor.py`) — the background process that owns the
  backend (uvicorn) + the voice listener as **windowless** children (`CREATE_NO_WINDOW`), health-
  monitors them (`GET /health`, not just "pid alive"), and **restarts either on crash with
  exponential backoff** (so a crash loop can't peg the CPU). A sustained-healthy stretch resets the
  backoff. If a backend is already serving :8000 (e.g. a dev `run_pwa`), it **attaches** instead of
  double-spawning. **Single-instance lock** (pid file, steals a stale lock) so logon can't start two.
  Logs to `database/runtime/{supervisor,backend,listener}.log` (rotating) since there's no console.
- **No-admin auto-start** (`scripts/jarvis_autostart.py`) — registers a per-user **Task Scheduler**
  task with a **logon trigger** that launches the supervisor under **pythonw** (console-less).
  Generated as a Task-1.2 **XML** (LogonTrigger · `LeastPrivilege` = no UAC · `Hidden` · unlimited
  runtime · survives on battery · Task-Scheduler restart-on-failure ×3 on top of the supervisor's
  own restarts). `--install [--tray] [--whatsapp] [--ngrok] [--no-voice]`, `--start` (run now, no
  reboot), `--status`, `--uninstall`, `--print-xml`. *Why a logon task, not an nssm service:* a session-0
  service is walled off from the user's mic/speakers/camera since Vista — a logon process isn't, and
  needs no admin. This is the correct, free, full-device-access path for an always-listening assistant.
- **System-tray app** (`scripts/jarvis_tray.py`, optional) — an arc-reactor icon whose colour is the
  live state (green listening · cyan muted · amber starting · red offline), with **Open JARVIS**
  (HUD in an Edge/Chrome `--app` window), **Mute/Unmute mic**, **Restart JARVIS**, **Quit JARVIS**
  (stops everything), **Close tray only** (JARVIS keeps running). **Killing the tray never stops
  JARVIS** — it only reads the supervisor heartbeat and writes the same file flags. `pystray` is the
  only new dep and is optional (headless runs fine without it); Pillow (already present) draws the icon.
- **File-based coordination** (`app/services/runtime/`) — supervisor, listener and tray talk through
  tiny atomic files under `database/runtime/` (status heartbeat, `mic.muted`, stop/restart requests,
  the lock) — restart-proof, socket-free, never raises into a hot path.
- **Mic-mute honoured by the listener** — the wake loop checks `runtime.is_muted()` (cheap, throttled
  to ~0.5 s) and, when muted, keeps draining the mic (stays the sole reader) but ignores the wake
  word AND the hands-free call-wake — a true mic-off, no half-states.
- **Full headless feature parity.** The supervisor runs the SAME backend + listener as `run_pwa`,
  in the user's logon session — so EVERYTHING works headless: voice, **face recognition** + voice
  ID (backend owns the camera in-session), vision, **calls**, messaging (`--whatsapp`), memory,
  tools, OS timers. The one phone-reachable gap (companion + mobile PWA need the public URL) is
  closed by `--ngrok`, which brings up the permanent tunnel as a managed, restart-on-crash child —
  so `--install --whatsapp --ngrok` gives the same surface as the dev `run_pwa --whatsapp --ngrok`.
- **UI untouched.** Always-on is an OS/background capability surfaced by the **native tray**; the PC
  HUD and mobile shell are byte-for-byte unchanged (no HUD element maps to it).

Verified: `scripts/autostart_smoke.py` **64/64** — runtime status/mute/stop/restart roundtrips,
process-liveness + single-instance lock (incl. stealing a stale lock), health probe, interpreter +
pythonw discovery, supervisor child-command building (spawn/attach/no-voice) + crash-loop backoff +
no-window flag, autostart task-XML (logon trigger, no-admin, hidden, restart-on-fail, on-battery,
correct interpreter/args, well-formed), tray state→colour + icon draw, and the listener mute wiring.
**Live "reboot → say wake up jarvis with nothing on screen" check is the one step left to Aditya**
(`--install` then `--start`, or just reboot). No regression to other smokes (pure addition).

## Phase 10.F — what shipped

**JARVIS speaks up on his own when it's earned** — a colleague at the next desk, not a chatbot
waiting to be addressed. All free/local. `app/services/proactive/`. (PLANNER §10.F / 10.F.1 / 10.F.2.)

- **Five triggers, evaluated server-side each poll** (priority high→low):
  1. **Routine pre-nudge** — 30–60 min before a `routines.*` block ("your gym block is in ~45 minutes,
     sir — want me to queue your playlist?"). Reads the times he set (Phase-4 semantic memory).
  2. **Call gap** — from the REAL Phase-8 call log + the contacts relationship map: "it's been ~N days
     since you and Mom spoke, sir — want to give Mom a ring?" (factual, never fabricated; silent if no log).
  3. **Hydration / break** — after ~90 min heads-down talking to him, "worth a quick water break?"
  4. **Routine check-in** — 20–90 min after a routine, *sometimes* (coin), "how'd the gym go?"
  5. **Idle chatter (10.F.1)** — a lull mid-conversation → an **LLM-composed, context-aware** remark
     pulled from what he's working on (or it returns `<SILENT>` and stays quiet — "a boring line is
     worse than silence"). The only trigger that calls the model; the rest are deterministic templates.
- **Never noisy — one auditable gate** (`engine._global_ok`): disabled/paused, **quiet hours**
  (23:00–08:00), the **active user isn't the Owner**, the **daily cap** (15), a **minimum gap** (10 min)
  between any two lines, and a **mood gate** — `vulnerable` → total silence; `frustrated`/`urgent` →
  only the brief, useful routine pre-nudge; everything **coin-flipped + jittered** so it's never clockwork.
- **Persistent** (`proactive.db`, WAL) — the cap, the gap, and per-trigger dedup ("one gym nudge per
  day", "one hydration prompt per 2h") **survive a restart** (an in-memory counter would let him repeat
  himself — finality rule). The pause/snooze switch persists too.
- **Listener integration, no latency cost** — the listener polls `/proactive/poll` every ~25 s with its
  state (in a conversation? idle how long?); the backend decides + returns a line; the listener speaks
  it under the existing speaking lock (never overlaps a reply/announcement). A nudge fired **outside** a
  conversation opens a brief **reply window** (the `_PROACTIVE_WAKE` path, mirroring the call-ring one)
  so he can answer hands-free, no "wake up jarvis". Idle chatter fires **inside** a conversation lull,
  where his reply is captured by the turn already listening.
- **He can shape it by voice** — `set_routine` ("my gym is at 6", "set my walk to 7:30am", "stop
  reminding me about the gym" → writes `routines.<name>_time` + a `.notify` opt-out) and
  `proactive_control` ("stop bugging me for an hour", "you can chime in again" → pause/snooze/resume).
  Both Owner-tier, persisted.
- **UI untouched** — proactive lines surface through the SAME speak→HUD path as any reply (the existing
  transcript/reply element mirrors them); no new panel, PC HUD + mobile byte-identical.
- **Deferred (documented):** the streak/skip observation ("three skipped gyms this week") — it needs a
  reliable did-he-actually-do-it signal we don't yet collect, so it's deferred whole rather than shipped
  guessing (finality rule). Everything else of 10.F / 10.F.1 / 10.F.2 is in.

### 10.F — adversarial audit + hardening (the "make sure it actually works" pass)

After the first build I ran a **25-agent adversarial bug audit** (5 reviewers across engine/listener/
store/wiring/runtime, each finding independently re-verified by a skeptic) PLUS a static compile/import
sweep and a **live end-to-end runtime test** (real router → real `engine.poll()` → real services). The
green smoke had hidden **13 real defects**; all are now fixed, each with its own regression test:

1. **call-gap used the number, never `c.name`** → most named callers (esp. spaced caller-ids) silently dropped. Now falls back to the phone name (mirrors `_nice_name`).
2. **call-gap counted missed/declined as "spoke"** → never nudged about someone you keep missing. Now `recent(kinds=(ANSWERED, OUTGOING))` only.
3. **24-h times with any suffix ("18:00 daily") failed to parse** → routine silently dropped. 24-h match is now unanchored.
4. **military time ("0730"/"1830") parsed to None.** Added a 3–4-digit HHMM branch.
5. **morning routines (<09:00) had their pre-nudge eaten by quiet hours.** Explicit routine nudges now **bypass quiet hours** (quiet still suppresses the unsolicited triggers).
6. **routines near midnight never fired** (event anchored to today). Now checks yesterday/today/tomorrow occurrences.
7. **`_proactive_loop` died permanently on one TTS/audio hiccup** → proactive silently dead for the session. Loop body is now exception-guarded.
8. **proactive nudge could collide with a live reply** (double-speak/cut-off). Non-idle nudges are now **deferred while a conversation is open** (idle-chatter only fires in a real lull, where idle_s ≥ 80 s guarantees no reply is playing).
9. **idle-chatter crashed if a work-fact value held a `{brace}`** (`str.format` KeyError). The `{user}` placeholder is now formatted on the static header only.
10. **weekday/"daily" qualifiers were ignored** → nagged on weekends. Routines now parse a recurrence (weekdays/weekends/daily/day-names) and skip non-matching days.
11. **a fire was counted before it was spoken** → a dropped/timed-out line burned the daily cap + armed the min-gap. Switched to **record-on-ack**: `poll` returns a candidate, the listener `POST /proactive/ack`s only after it actually speaks, and only then does it count.
12. (same root as #1.)
13. **the active-user had no TTL** → a guest's single message (even from the phone) left JARVIS believing a non-owner was present forever, silently muting the Owner's desktop nudges. `get_active()` now **expires a stale non-owner back to the Owner default** (also fixes the HUD).

The audit also **refuted 8 plausible-but-not-real** claims (e.g. "orb stuck after a nudge" — a 25 s
HUD safety-timer already handles it; "idle MAX window unreachable" — inert, not a fault), so they were
left alone rather than churned.

Verified: `scripts/proactive_smoke.py` **71/71** (includes an explicit regression test for each of the
13 bugs above) + a static compile/import sweep + a **live e2e** (real backend fires the gym nudge over
HTTP, ack dedups it, zero DB residue). No regression: identity 84/84, calls 61/61, mobile 28/28,
always-on 64/64 still green. **Live spoken-nudge check (hear him pipe up) is the one step left to Aditya.**

## Phase 10.B — what shipped

**JARVIS watches what the boss cares about in the background and flags significant changes** — and
gives a spoken briefing on demand. All sources free / no-key / no-card. `app/services/feeds/`.
(PLANNER §10.B.)

- **Persistent watchlist** (`feeds.db`) — crypto, stocks, GitHub repos, subreddits, news keywords,
  a city's air quality, earthquakes near a city. `watch`/`unwatch`/`list_watches` by voice ("keep an
  eye on Solana", "stop watching Tesla"). Survives restart.
- **Background anomaly monitor** (`monitor.py`, every `FEEDS_POLL_S`=3 min) — per kind: a **price move**
  ≥ threshold within a true **rolling window** ("Bitcoin's down 8% in 20 minutes, sir"), a **GitHub
  star jump**, a **new** reddit/news headline (first successful fetch only seeds — never spams a
  backlog), **AQI** crossing into unhealthy, and a **new earthquake** within range of a watched city
  (**critical** — bypasses quiet hours, alarm-toned). Guards: per-alert cooldown, and quiet-hours
  hold for the non-critical alerts (still logged + shown on the HUD, just not spoken at 3am). Snapshots
  persist so a restart doesn't re-alert on a forgotten baseline.
- **On-demand briefing + market check** — `whats_happening` ("brief me / what's the world up to") gives
  a natural ~30-second spoken digest (his markets + city weather/air + top headlines), and
  `market_check` answers a single "what's Bitcoin / AAPL at?" — live numbers only, honest-miss if a
  source is down, never fabricated.
- **Free sources** (`sources.py`, each fails soft, none needs a key): CoinGecko (crypto), **Stooq**
  (stocks), **open-meteo** (weather + US AQI), **USGS** (quakes), GitHub (optional token only raises
  the rate limit), Reddit, HN, **Google-News RSS** (keyword news).
- **Alerts ride the proven path** — `monitor.drain()` → the desktop listener's `_feeds_alerts_loop`
  speaks them (chime + speaking lock, exception-isolated so one TTS hiccup can't kill the loop), same
  as the message/call announce loops. Router `/feeds/{alerts,briefing,dashboard,watchlist,watch,unwatch,market}`.
- **UI untouched** — feeds are voice-first + the `/feeds/*` endpoints; the **Phase-9 HUD ticker is left
  byte-identical** (per the UI rule), with `/feeds/dashboard` available for a future wiring if wanted.

### 10.B — adversarial audit + hardening

Same rigour as 10.F: a **16-agent adversarial audit** (5 reviewers across monitor/sources/store/
briefing/wiring, each finding re-verified) + static sweep + a **live e2e** (real `/feeds` router → real
monitor → LIVE CoinGecko: plant a baseline, run a real sweep, a genuine >5% move fires an alert that
drains over `/feeds/alerts`; zero residue). The audit **refuted 2** plausible-but-not-real claims (a
zip-misalignment that can't occur given current sources; a cross-thread DB race that can't occur because
the tool thread blocks the event loop) and **confirmed 9 real bugs**, all now fixed with a regression test:

1. **`reddit_new` used `lstrip("r/")`** → mangled any subreddit starting with r ("rust"→"ust"). Now a real prefix strip.
2. **A failed FIRST fetch seeded an empty baseline** → the next successful poll alerted on every pre-existing item, incl. a **false CRITICAL earthquake alert** that bypasses quiet hours. Sources now return `None` on a fetch failure (vs `[]` for genuine-empty); the monitor only seeds on a real success.
3. **Price window was tumbling with a hard reset** → a move straddling a window boundary was missed. Now a true rolling window (compares vs the oldest sample still in the window).
4. **`remove_watch` used `target OR label`** with no kind → could silently delete the wrong/extra watch (a stock labelled "Tesla" when unwatching a "Tesla" news keyword). Now target-first, with a friendly-label fallback only when unambiguous.
5. **RSS/news titles kept raw HTML entities** ("AT&amp;T") and the `<title>` regex rejected attributes (Atom feeds yielded nothing). Now `html.unescape` + attribute-tolerant.
6. **Stock change was vs the OPEN, not prev close** but spoken as "on the day". Relabelled "since the open" (honest).
7. **A null/short USGS geometry crashed the loop and dropped the WHOLE quake list.** Per-feature guard + try/continue.
8. **`_user_city` opened memory.db with no `busy_timeout`** → a concurrent write made the briefing silently use the wrong city. Now waits out the lock.
9. (same root as #1, second reviewer.)

Verified: `scripts/feeds_smoke.py` **65/65** (a regression test for each bug above — rolling window,
seed-on-failure, reddit prefix, RSS entities/Atom, remove-watch collision, null-geometry, etc.) +
static sweep + live e2e. No regression: proactive 71/71, calls 61/61, identity 84/84, messaging 59/59,
always-on 64/64. **Live check (set a watchlist, hear an alert / ask "brief me") is the step left to Aditya.**

## Phase 10.A — what shipped

**JARVIS goes off on his own, reads many sources across the web, cross-references them, and comes
back with a synthesized briefing — while he's still talking to you.** All free. `app/services/research/`.
(PLANNER §10.A.)

- **The Deep Researcher** (`engine.py`) — a real four-stage pipeline: (1) an LLM **decomposes** the
  topic into focused sub-questions; (2) **gathers** — Tavily discovers sources per question (basic
  depth, to conserve the free quota), httpx + **trafilatura** read the main text, **multi-hop** follows
  cited links a level deeper, with a **headless Playwright** render falling back for JS-heavy pages;
  (3) every page is chunked + embedded into a **transient in-memory FAISS index** (the local MiniLM —
  no key, no cost) and the most relevant chunks are selected (lexical fallback if the embedder's down);
  (4) an LLM **synthesizes** a structured briefing — a spoken digest, executive summary, cited key
  findings, contradictions, and a **confidence rating** — over ONLY those chunks, with **source-trust
  grading** (gov/edu/Wikipedia/known press > blogs) weighting both selection and the briefing.
- **Never blocks the conversation** (`manager.py`) — sweeps run on a **dedicated background worker
  thread** with its own asyncio loop. `deep_research` kicks one off and returns at once; JARVIS says
  he's on it in one line and carries on. Admission control caps concurrent sweeps, dedups the same
  topic, and self-heals an orphaned slot. Briefings persist in `research.db` (latest-N per topic, no
  unbounded growth) + a recallable digest into episodic memory.
- **Live narration + delivery** — the listener's `_research_loop` drains `/research/progress` (a
  mid-sweep "N sources read, cross-referencing now", spoken plainly) and `/research/done` (the finished
  briefing, spoken with a soft chime), riding the same speaking-lock path as the other announce loops.
  `research_status` ("how's that going?"), `read_briefing` ("what did you find / read me that briefing").
- **Continuous topic monitoring** (`monitor.py`) — "keep watching X" re-runs the sweep on its own
  cadence (`watch_topic`/`unwatch_topic`/`list_research_topics`), **seed-then-alert** (no false "it
  changed!" on the first pass), and speaks up only on a **material change** (a key-findings fingerprint
  diff), held during quiet hours, bounded per tick.
- **Free + bounded everywhere** — Tavily free tier + local embeddings + the LLM key rotator; source
  cap, per-host cap, browser cap, an overall time budget enforced per fetch-wave, and a synthesis
  payload kept small enough that the rotator never 413s (and now skips a too-big provider on a 400
  context-length error too, not just 413).
- **UI untouched** — voice-first + the `/research/*` endpoints (`/dashboard` available for a future HUD
  wiring); no UI element changed (per the UI rule).

### 10.A — adversarial audit + hardening

A **34-agent adversarial audit** (6 reviewers across concurrency/engine/fetch/store/integration/
free-cost, every finding re-verified by an independent skeptic): **28 candidates → 24 confirmed, 4
refuted**, all 24 now fixed with regression tests. The notable ones:

1. **`_host()` used `.lstrip("www.")`** (both `fetch.py` + `engine.py`) — strips CHARACTERS not a
   prefix, so `wikipedia.org`→`ikipedia.org`, `wsj.com`→`sj.com`, `who.int`→`ho.int`: the most common
   research sources were silently mis-graded/mis-labelled on **every** sweep. Now a real prefix strip.
2. **Main conversational reply never took `_speaking_lock`** (pre-existing, but research made it bite)
   — a background line could play over / chop off a live reply. The main reply + greet now hold the
   same mutex as every announce loop, so audio can never overlap.
3. **`chat_stream` only treated 413 as "too large"** — a 400 "context length/tokens" (the big-synthesis
   case) tripped the breaker and benched Groq for ordinary chat. Now it skips the provider like 413.
4. **Per-wave time-budget enforcement** — an in-flight slow wave could overshoot the ceiling; each wave
   now bounded by the remaining budget (completed reads harvested, the rest cancelled).
5. **`fetch_url` streamed with a size + content-type cap** (no more pulling a huge blob into memory),
   and trafilatura/lxml parsing moved **off the worker loop** (`to_thread`) so concurrent sweeps
   aren't serialized.
6. **Store**: SQLite reads now hold the lock (shared connection across worker + backend threads), LIKE
   wildcards (`%`/`_`) escaped in the fuzzy briefing/monitor lookups, and briefings pruned to latest-N.
7. **Manager**: race-free worker-loop creation, `run_blocking` registers as active (bidirectional dedup
   with the monitor), orphaned-slot reaper. **Embedder**: `encode()` serialized against concurrent sweeps.
8. Plus: Tavily `advanced`→`basic` (free-quota), monitor writes off the event loop + capped per tick,
   `_split_spoken` no longer eats the EXECUTIVE header, `_diversify` dedups by identity, Playwright
   disables itself if the Chromium binary is missing, `read_briefing` kept under the tool-result cap,
   `watch_topic` cue-words.

Verified: `scripts/research_smoke.py` **109/109** (offline suite incl. 16 audit-fix regression checks +
a **live end-to-end sweep**: real Tavily → trafilatura → MiniLM/FAISS → LLM synthesis → a grounded,
cited briefing). No regression: feeds 65/65, memory all-pass. (Proactive shows 70/71 only because that
10.F **test** hardcodes midday and was run after midnight — the 10.F engine is correct, unrelated to
10.A.) **Live voice check (say "do a deep dive on X", hear the briefing land) is the step left to Aditya.**

## Phase 11 — what shipped

Identity, recognition & access control. **Only the Owner gets the full JARVIS; enrolled people get
limited access; unknown voices get nothing.** All free/local. `app/services/identity/`.

- **Two biometric factors, both free & local:**
  - **Voice** — `resemblyzer` 256-d voiceprint (bundled ~40 MB weights, CPU, no key). Real test:
    same speaker 0.89, different speaker 0.57 — cleanly split by the 0.75 cosine gate.
  - **Face** (optional 2nd factor) — **OpenCV YuNet (detector) + SFace (recogniser)**, 128-d, both
    free ONNX models auto-downloaded from the OpenCV Zoo. **No `dlib`** (won't build on Windows) —
    this is the Windows-native path. Used only when a camera frame is on hand → ~0 per-turn cost.
- **Encrypted at rest** — every voiceprint/face vector + the passphrase is sealed with the
  **Windows DPAPI** via a `ctypes` shim (`crypto.py`) — OS-keystore-grade, tied to the Windows
  user, no `pywin32`/`keyring` needed. Fernet fallback off-Windows.
- **Trust tiers + per-tool gating** — Owner / Trusted / Guest / Stranger. Each tool declares a min
  tier (`@tool(tier=…)`); the access policy lives in ONE auditable table (`app/tools/__init__.py`):
  guest = web_search/weather, trusted = timers/media, **everything personal/communicative stays
  owner**, `remove_access` = **owner+passphrase**. The agent filters the tool schema to what the
  speaker may SEE, and re-checks at execution (backstop) — a below-tier call is refused in character;
  an owner+passphrase call with no phrase makes JARVIS ask for it.
- **Spoken passphrase** — the most destructive ops need the Owner's configured phrase (matched on
  normalised speech, stored encrypted). Set via `set_security_passphrase` or the enrol CLI.
- **Continuous re-verification, ~0 latency** — the voice listener computes the voiceprint **in
  parallel with STT** (like the emotion read), so it hides under the STT round-trip. Trust is cached
  per channel for 10 min; a too-short clip reuses it instead of locking the Owner out. An unknown
  voice gets a polite deflection and **never reaches the brain**. The encoder is pre-warmed at
  listener start so the first command isn't slowed.
- **Memory stays private** — a trusted/guest/unknown speaker's turns are never written to the
  Owner's memory, and his private memory is never surfaced to them.
- **Enrolment** — `python scripts/enroll_identity.py owner|trusted <name>|guest <name>|list|remove|
  passphrase` (records the mic + optional webcam, enrols locally/encrypted). Also token-gated
  `/identity/{enroll,verify,remove,passphrase,roster,status}` for the app.
- **Open mode until you enrol** — with no Owner enrolled, JARVIS answers everyone exactly as before;
  gating only switches on once you enrol your own voice. Persona block keeps refusals in character
  and never leaks the mechanism.

### Phase 11.1 — live recognition, guided enrolment & UI sync

- **Mandatory face gate at startup** — when an Owner is enrolled, the listener asks the backend
  (which owns the camera) to scan a face before the wake loop, then **greets by name**: "Face
  recognised. Welcome back, sir." / "Hello, Vikram." / the stranger line. No camera → silently
  falls back to per-turn voice. The wake greeting is also name-aware.
- **Voice-guided enrolment** — *"JARVIS, add Vikram as trusted"* opens a guided session: JARVIS
  has them look at the camera (face captured via the backend) and read three lines (voice), then
  enrols them at the chosen tier — all hands-free. (`enroll_person` tool → pending session →
  listener dialog → `/identity/enroll/*`.) The CLI/app paths still work too.
- **"Run face recognition again"** — `reverify_user` (guest-tier) re-scans the webcam and switches
  the active user, so when a different person takes over (or you come back), JARVIS re-identifies
  and updates everywhere. Owner stays owner-only to enrol.
- **Active user synced to BOTH UIs** — a new `identity` event on the bus (pushed on every verified
  turn + on a scan) drives the **PC HUD's top-left IDENTITY panel** (name · tier, the biometric %,
  the avatar initial, VERIFIED/UNVERIFIED) and the **mobile header** (avatar initial + name·tier).
  `GET /identity/active` seeds it on load. The panels default to the original static text, so the
  PC HUD is byte-identical until a real identification arrives.
- **Four positions (tiers):** Owner (everything) · Trusted (chat/questions/weather/media/own timers;
  no acting-as-owner, no private data) · Guest (Q&A only) · Stranger (deflected — *"you're a stranger
  to me, not on Aditya sir's list of trusted people"*).
- **Deferred (documented):** full per-user memory namespaces (writes are already owner-gated).

**Adversarial review (5 reviewers + per-finding verifiers):** PC-HUD-byte-identical ✅, zero-latency ✅,
no regressions ✅. The headline "tier-spoofing bypass" was **refuted** (non-voice surfaces already
default to owner, so a forged tier can't escalate). Two real issues were **confirmed and fixed**:
(1) the owner+passphrase gate no longer silently degrades to plain owner when no passphrase is set —
it stays closed until one is configured AND spoken (regression-tested, smoke now **73/73**);
(2) guided-enrolment now embeds outside the lock and refuses to clobber an in-progress session.
### Phase 11.2 — recognition & enrolment on the PHONE (smoke 84/84)

The phone now does its OWN biometric recognition + enrolment (not just mirroring the PC), which
also tightens the web surface the earlier review flagged:

- **Server-verified mobile voice gating.** Each phone command's mic audio is sent to the open,
  read-only `POST /identity/whoami` (with the PWA session id) IN PARALLEL with STT — the SERVER
  does the voiceprint (so a client can't forge a tier) and stashes the verified tier per session.
  `/chat` reads that. It is **downgrade-only**: a recognised friend/stranger gets their lower tier
  (a stranger is deflected on the phone with the in-character line), but a miss never locks the
  Owner out — absence defaults to Owner. The header name-panel updates to whoever's recognised.
- **Guided enrolment ON the phone.** After the Owner (verified) says "add Vikram as trusted",
  the phone auto-runs the capture — a front-camera selfie + three spoken lines — and enrols them,
  reusing the mic + camera. Authorised by the Owner's **verified session** (`_auth_enroll`), so the
  master token is never shipped to the browser; a guest/no-auth request is refused (401).
- Both fixes from the review are in (passphrase gate hardened; enrolment lock/race fixed). The
  one remaining item — the PWA `/chat` defaulting to Owner for an *un-voiced* request — is now
  mitigated: when the phone hears a non-Owner it downgrades, and the Owner is greeted/verified by
  voice. A raw, silent HTTP client with the URL still gets Owner (the URL remains the secret); a
  hard PIN lock is available on request.
- **Smoke 84/84** (`scripts/identity_smoke.py`): whoami owner/stranger/trusted, downgrade-only
  /chat gating, owner-session-authorised enrol endpoints (guest/no-auth → 401), full mobile
  enrolment. Mobile JS passes `node --check`; PC HUD verified byte-identical; calls 57/57, mobile 28/28.

Verified: smoke **56/56**, real voiceprint same/different separation, DPAPI roundtrip + tamper
rejection, tier matrix, passphrase gate, schema-visibility vs execution backstop, router auth,
persona block, and `calls_smoke` still 57/57 (no regression). Open mode confirmed on the live store.

## Live-tuning fixes (2026-06-23, from Aditya's first real voice sessions)

Three issues surfaced live and were fixed (all smokes still green):

- **Groq `413 Payload Too Large` on every action turn → fell back to weaker Cerebras (robotic,
  impersonal answers + latency).** Root cause measured: each action request was ~15k tokens —
  persona ~5.5k + 89 memory facts ~1.3k + **all 68 tool schemas ~8.3k**. Fix: **relevance-based tool
  selection** (`tools.for_openai(relevant_to=user_text)`) — only the core set + tools whose
  name/description/cue-words match the utterance (capped 14) are sent, so the tools drop ~8.3k→~1.5k
  and the request falls to ~7.7–9k, which fits Groq. **Persona + all memory are untouched** (so
  personalization/emotion are unchanged — they now come through Groq, the better model, instead of
  the fallback). Plus a rotator **learned-skip**: if a provider still 413s a tool request, it's
  skipped for the rest of the session instead of being re-tried (and re-logged) every turn.
- **Voice ID flapping (recognised Aditya one turn, "unrecognised — deflecting" the next).** The gate
  had no middle ground (clear it, or be branded a stranger). Added an **"unsure" band**
  (`IDENTITY_VOICE_FLOOR`=0.60, gate lowered 0.75→0.70): a believable-but-imperfect clip reuses the
  established trust instead of deflecting; only a clearly-different voice (~0.57, below the floor) is
  a stranger. (identity smoke 85/85, incl. a borderline-band regression test.)
- **Feed alerts read raw headlines** ("…raises $98 million - CNBC"). Now framed in JARVIS's voice
  ("A fresh headline on AI — …, sir.") with the trailing "- Publisher" stripped.

## Phase 3 — what shipped

- **KeyRotator** (`app/services/llm/key_rotator.py`) — quota-aware (SQLite `database/keys.db`,
  per-key/per-day, survives restart), task-aware routing (chat/vision/embed/stt), 429/5xx
  circuit-breakers, per-key→per-provider fallback.
- **Provider catalog** (`app/services/llm/providers.py`) — one OpenAI-compatible adapter for
  Groq, Gemini, OpenRouter, Together, Cerebras, Mistral; STT via Groq Whisper + Deepgram.
  Empty providers (Together/Cerebras/Mistral) auto-activate when a key is added — no code change.
- **`LLMService` / `STTService`** refactored onto the rotator — Phase 1 voice loop public API
  unchanged.
- **`GET /admin/key-stats`** — live quota usage per provider/key.

Active now: chat = groq(6) → gemini(1) → openrouter(5); stt = groq(6) → deepgram(5).

**Action for Aditya (optional, strengthens the free backup tier):** your single **Gemini key
is currently quota-exhausted** and OpenRouter's free model is often busy upstream — so Groq's
6 keys do the real work (~84k req/day, plenty). To harden the backups, add free keys (no card):
more `GEMINI_API_KEY_2/3` (extra Google accounts), and `CEREBRAS_API_KEY` / `TOGETHER_API_KEY`
/ `MISTRAL_API_KEY`. They light up automatically.

## Phase 7 — what shipped

Messaging across **WhatsApp · Instagram · Email** — all free, no credit card. (Telegram
**dropped** at Aditya's request; he uses it manually.)

- **Email (Gmail, free App Password — IMAP read + SMTP send):** `email_client.py`. Read unread
  with `BODY.PEEK` (reading never marks mail read), send, and **proper threaded replies**
  (In-Reply-To/References). Chosen over OAuth deliberately — no Cloud project, no consent
  screen, no card (RULES §4). **Live-verified**: real Gmail IMAP login + fetch in the smoke test.
- **Instagram (instagrapi, unofficial, free):** `instagram.py`. Session persisted to disk
  (`database/ig_session.json`) so we log in once and reuse the device fingerprint — the main
  ban-avoidance. Read **who DM'd / who has a story up / who liked your post / who viewed your
  story / post comments / account & profile lookups**; **act**: like, comment, follow/unfollow,
  send DM, **publish a feed post**, **add a story**. Gentle 2.5 s min-interval + jittered 15-min
  poll + graceful challenge/2FA handling (degrades to a clear message, never crashes).
- **WhatsApp (local Node sidecar, `sidecars/whatsapp/` — whatsapp-web.js):** QR-paired once,
  session persists (`.wwebjs_auth/`). Sidecar exposes `/status /inbox /chat /send /read` and
  **pushes incoming messages** to the backend webhook (token-gated). Python side
  `whatsapp_client.py` (async + sync). Read inbox, read a specific chat, send to person/**group**,
  mark read. `npm install` done; `node --check` passes. `run_pwa.py --whatsapp` starts it.
- **Unified, persistent cross-channel inbox:** `store.py` (SQLite `database/messaging.db`, WAL,
  dedup via `UNIQUE(channel,ref)`, survives restart). **LLM importance classifier** (`classifier.py`,
  free key-rotated, heuristic fallback) tags each inbound high/normal/low + a one-line gist.
  `unified.py` merges + ranks by importance then recency.
- **Proactive but not noisy:** only **important** new messages are announced — spoken on the PC
  (the listener drains `/messaging/announcements`) and shown on the HUD (`notify` event). Everything
  else lands quietly in the inbox. WhatsApp is instant (push); email 5 min; IG DMs ~15 min.
- **Mute:** `mute_chat`/`unmute_chat`/`list_muted` — a muted contact/group is stored but **never
  announced and hidden from the digest** (still findable on explicit lookup). Mute beats everything.
- **Reply on his behalf — ONLY on command:** `reply_to_messages(channel, instruction?, contact?)`.
  No silent background auto-responder (removed by design after Aditya's note). He says "reply to my
  WhatsApp messages" (all) or "reply to Vikram" (one); JARVIS drafts + sends a reply per
  conversation that **always identifies itself as JARVIS, his assistant** (hard-guaranteed, never
  impersonates Aditya). Incoming messages alone trigger nothing.
- **PWA HUD wired:** the bottom-right **UNIFIED COMMS** panel (06) + its "NEW" badge now pull real
  data from `/messaging/inbox` (WA/IG/email, colour-coded, importance dot), refreshing every 20 s
  and instantly on a new-message event. Built from `JARVIS.html` (design untouched); patched
  component passes `node --check`.
- **Tools (22):** read_emails, send_email, reply_email, read_whatsapp, read_whatsapp_chat,
  mark_whatsapp_read, send_whatsapp, instagram_activity, send_instagram_dm, instagram_like,
  instagram_comment, instagram_follow, instagram_profile, instagram_post, instagram_add_story,
  unified_inbox, messaging_status, mute_chat, unmute_chat, list_muted, reply_to_messages,
  set_autoreply_rule. Every tool degrades in character when its channel isn't connected.

Verified: `scripts/messaging_smoke.py` — **58/58** (store/dedup/persistence, mute silence + digest
hiding, classifier heuristic, unified ranking, spoken-announce buffer, WhatsApp webhook path +
dedup, **no background auto-reply**, reply identity guard, all 22 tools register, graceful degrade,
**live Gmail IMAP**, persona block). Sidecar `node --check` passes; full app imports + mounts.

### Phase 7 — final hardening (session 2, all live-verified by Aditya, smoke now 59/59)

- **All three channels live:** Gmail (app password), Instagram (instagrapi via saved `sessionid`,
  never password-login — validates session w/ `account_info`, negative-cache backoff), WhatsApp
  (sidecar QR once, session persists, graceful shutdown so no QR re-scan on restart).
- **Per-channel nicknames** (`MY_CONTACTS.txt` `[whatsapp]`/`[instagram]`/`[common]`): the boss's
  word maps to the real saved name **per app** (different on WA vs IG). Scored matching in the
  sidecar (exact > starts-with > all-words > substring). Persona forbids substituting a remembered
  real name (was sending to the wrong/ random person). "my sister"/"co-founder" prefixes handled.
- **Send = exact words vs gist:** profanity/quoted lines relayed **verbatim** (messenger reframe —
  never refuses directed insults to his own friends); casual intent ("ask how he's doing") is
  **composed** into a natural message, not the literal STT scrap.
- **Context-reply** (`compose_reply`): opens the person's own chat, reads ~18 msgs, drafts a reply
  to **their last message** in his voice, and **states what it read** so he can trust it. Routed
  explicitly (never the inbox digest, which misses people not at the top).
- **Group-aware mute:** the sidecar now sends the group **name** + the backend stores the group as
  the conversation identity (member shown in the preview), so muting a group by name actually hides
  it (Ashoka group fixed — 248 backlog msgs retroactively muted by chat-id).
- **Delete/unsend** (WA + IG), **IG**: likes / who-liked / who-viewed-story / post / story / comment
  / follow / profile. On-demand bulk reply (`reply_to_messages`, identifies as JARVIS).
- **Speed:** terminal-action **fast-path** (skips the 2nd LLM "rephrase" turn for send/delete/like
  /etc.), sidecar **resolveChatId + contacts cache** (killed a 36s name-scan per op), persisted IG
  `username→user_id` cache, startup pre-warm of IG session + WA chat cache.
- **LLM routing:** rotator **skips a provider on 413** (Groq's free per-request cap) instead of
  storming all 9 keys + tripping breakers → straight to Cerebras. **Tool-gating** — chit-chat sends
  no tool schema so it fits Groq (fast, 9 keys); action requests get tools (Cerebras); one-shot
  retry-with-tools if a chit-chat guess actually wanted a tool. 27 chat keys (groq/cerebras/gemini/
  mistral/openrouter).
- **STT upgraded:** **Deepgram nova-3** is now primary (tracks fast natural speech far better than
  Whisper), Groq Whisper + local faster-whisper as fallbacks.
- **HUD voice sync fixed:** listener emits HUD events through one ordered queue and owns `idle`
  (only on real exit) — no more standby-flicker after a reply; a too-brief blip keeps the
  conversation open instead of dropping to wake-word.

### Deferred from Phase 7 (with reasons)

- **WhatsApp calling** — `whatsapp-web.js` has no call API (calls aren't part of the WhatsApp Web
  protocol). No free, reliable way to place a WhatsApp call programmatically → deferred (would need
  a paid telephony/SIP path, which violates RULES §4). Messaging/groups fully covered.
- **Email auto-send to non-whitelisted addresses** — stays draft-and-notify by design;
  `reply_to_messages` is WhatsApp/Instagram only so the inbox is never mass-sent by accident.

**One-time setup for Aditya:** Gmail is already live. Instagram creds are in `.env` (the first
login may ask you to confirm it once in the IG app — instagrapi then reuses the saved session).
WhatsApp: `cd sidecars/whatsapp && node index.js`, scan the QR once (or `run_pwa.py --whatsapp`).

## Phase 5 — what shipped

JARVIS reads the boss's mood — from his **words AND his voice tone** — and calibrates his tone,
wit and even his speaking voice to match. All free, local, fast (no per-turn LLM call).

- **Situational awareness (`emotion/detector.py`):** a small local emotion model
  (`j-hartmann/emotion-english-distilroberta-base`, ~330 MB, CPU, no key) + cheap text heuristics
  fuse into one **register**: playful · sarcastic · frustrated · urgent · vulnerable · showing-off ·
  neutral. Priority protects distress first (explicit "asap" beats a model-inferred anxious tone;
  vulnerability/urgency always pre-empt humor). Live-tested 7/7.
- **VOICE TONE (`emotion/voice.py`, `POST /voice/emotion`):**
  `audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim` (free, local) reads emotion from HOW he
  speaks — so a flat "I'm fine" said angrily registers as upset, like a human hears it. Trained on
  **MSP-Podcast (real, natural conversational speech)**, so it generalises to everyday talking
  instead of over-predicting "neutral" like the acted-corpus (IEMOCAP) models — it outputs the
  dimensional arousal/dominance/valence axes, mapped to angry/sad/happy/neutral (lighter
  categorical model as fallback). The listener POSTs the utterance WAV **in parallel with the STT
  call** (both only need the audio), so inference (~0.5–1.5 s) hides under the transcription —
  **~0 added latency**. Voice tone overrides only a flat/neutral text read, never explicit intent.
- **Wit engine (`emotion/state.py`):** each register maps to a 4-axis mood (warmth/playfulness/
  urgency/focus, EMA-smoothed so it eases not whiplashes), a **humor budget** (0–1), a sampling
  temperature, TTS prosody, and a line of guidance injected into the system prompt every turn. The
  stable JARVIS humor profile (dry, understated, callback humor, "knows when to shut up") rides
  along, plus a recent-lines list so he never recycles a quip. Laughter from the boss is logged as
  a **humor hit** (what lands with him).
- **Prosody:** the reply carries a per-register rate/pitch; Edge-TTS colours JARVIS's voice — wit a
  touch livelier/higher, distress slower/softer, urgency faster. `synthesize_pcm(text, rate, pitch)`.
- **Per-turn temperature** scales with register (playful warmer/looser, urgent/vulnerable tighter).
- **HUD:** the EMOTION panel (register + warmth/playfulness/urgency/focus + humor bar) now reflects
  his **real** live read (pushed on a `mood` bus event), not the old mode-based mockup.
- **Wired:** runner injects the mood block + uses the temperature + tags the reply with the
  snapshot; `/chat/stream` passes voice-emotion in and broadcasts mood out; both emotion models
  warm at startup. Config flag `JARVIS_EMOTION` (default on). Degrades to heuristics/neutral if a
  model can't load — never crashes a turn.

**Deferred (with reason):** the 20-question onboarding wizard — `MY_PROFILE.md` (89 facts) already
*is* the deep personalization, so a wizard would only duplicate it; humor/tone prefs live there too.
Longitudinal pattern-spotting ("tired every Monday") belongs with the Phase-10 autonomous behaviours
(needs weeks of data + the consolidator), not here.

Verified: `scripts/emotion_smoke.py` **15/15**; messaging 59/59 and vision 11/11 still green
(rotator `prefer`/temperature changes are backward-compatible).

## Phase 8 — what shipped

JARVIS knows about and controls **phone calls** through a free Android companion — no telephony
fees, no SIP, no carrier change. He announces who's calling, declines/silences/answers on your
word, reports missed calls, and (8.5) auto-handles whitelisted callers.

- **Backend** (`app/routers/calls.py`, `app/services/calls/`): token-gated companion webhooks
  (`/calls/incoming` · `/missed` · `/ended` · `/sync` · `/commands` · `/rules`), a PWA/voice
  command endpoint (`/calls/command`), and a spoken-announcement drain (`/calls/announcements`).
- **Persistent call log** (`calls.db`, WAL): every event survives restart, deduped by ref, so
  "any missed calls?" works hours later. Newest-first reads, missed-unseen tracking.
- **Live-ring command queue:** when he says "decline"/"silence"/"answer" (tool) or taps the PWA
  card, the command is queued; the companion long-polls and executes it via `TelecomManager`.
  Guarded by a **ring TTL** (`CALL_RING_TTL_S`, 45 s) — a command only fires while it's actually
  ringing; a rung-out call honestly reports "no call ringing".
- **Number → name** via the Phase-7 contacts map (`[call]`/`[common]`), so callers are announced
  by the name he uses.
- **Tools (4):** `phone_call_action` (decline/silence/answer/read_missed/recent — terminal,
  never fabricates), `place_call` (outbound dial — "JARVIS, call Mom"), `set_call_rule`,
  `list_call_rules`.
- **Hands-free during a ring:** an incoming call now acts as the wake — the desktop listener
  opens a short window (the ring IS the wake), so the boss just says "decline"/"answer"/"silence"
  with NO "wake up jarvis". Implemented without a mic-race: `wait_for_wake` short-circuits to a
  call-capture turn while staying the single mic reader; reuses the backend command normalisation,
  and non-command speech ("who is it?") falls through to the normal brain. PWA tap card still works.
- **Outbound dialing (`place_call`):** the companion places a real call on the phone via
  `TelecomManager`/`ACTION_CALL` (free, no telephony fee) — JARVIS dials, the boss talks on the
  phone. Number comes from the `[call]` contacts map (same map used to announce by relationship).
- **Number → relationship name:** `MY_CONTACTS.txt [call]` maps a phone number to what JARVIS
  calls them ("Mom = 9871521319"), substring-tolerant so the 10-digit form matches a +91 caller id.
- **Phase 8.5 — auto-handle rules:** per-contact `auto_text` (decline + SMS "I'll call you
  back"), `auto_answer` (pick up on speaker), `auto_decline`. Stored in `calls.db`, pulled +
  cached + enforced on the phone by the companion.
- **PWA:** a body-level **incoming-call card** (ANSWER / DECLINE / SILENCE → `/calls/command`),
  driven by the live `call` event on the existing bus — patched component compiles through Babel,
  HUD design untouched.
- **Desktop listener:** a calls-announce loop (polled every 3 s) speaks the ring/missed line with
  a ring chime, using the same speaking lock so it never overlaps another announcement.
- **Companion — both paths** (`companion-android/`): a **Macrodroid recipe**
  (`macrodroid-recipe.md`, zero-code, testable in minutes) AND a real **Kotlin app** (foreground
  service, `TelephonyCallback`/`PhoneStateListener`, `TelecomManager` decline/answer, `ACTION_CALL`
  outbound dial, `CallLog` missed reads, `SmsManager` auto-text, always-on command poll,
  boot-restart) with a build/SETUP guide.
- **Persona:** a CALLS block — announce, act only on his word, can dial out (not speak on the
  line), never fabricate a call.

**Deferred (with reason):**
- **JARVIS talking *on* the call** (greeting + voicemail + full conversation) — Android blocks
  third-party access to the call voice stream (capture *and* injection) on 10+ without root, so
  the PLANNER's "AudioTrack greeting + record voicemail" can't be done that way. The genuinely
  free route is **PC-as-Bluetooth-handset** (the phone routes call audio to the PC over the
  Hands-Free profile; PC does STT→LLM→TTS) — a separate build that needs Aditya's phone paired
  and is verified with his hardware. Auto-handling today gives the practical equivalent
  (instant "I'll call you back" text / hands-free speaker answer).
  (Auto-answer onto a Bluetooth-paired PC is the entry point for that conversational build.)

Verified: `scripts/calls_smoke.py` **57/57**; messaging 59/59, vision 11/11, emotion 15/15 still
green (config additions are backward-compatible). Live phone test pending the one-time companion
setup.

## Phase 2 — what shipped

JARVIS can **see** — on demand, three ways, all free (no card), routed through the existing
KeyRotator vision task. **Groq Llama-4 Scout primary** (fast LPU, Aditya's pick — Gemini can get
congested), **Gemini 2.5 Flash** the accuracy fallback, then Together / OpenRouter vision.

- **`read_screen`** — `mss` grabs the PC screen → downscaled JPEG → vision LLM. "what's on my
  screen", "read this error", "what does this say". Screen reading **prefers Gemini** (best at
  dense text/OCR) but **falls back to Groq instantly** if it's busy — best accuracy *and*
  reliability. Live-verified (identified VS Code + read on-screen text).
- **`look`** — OpenCV grabs a **webcam** frame for hands-free "what is this, JARVIS?" / "what am I
  holding". Robust, quality-first capture: probes DSHOW→MSMF→default backends across indices
  (caching the one that works so later shots are fast), requests 1280×720, lets auto-exposure/focus
  settle (~1.2s — webcams start black/hunting), then keeps the **sharpest well-lit frame** of
  several candidates (kills the dark/blurry shots that made the model say "I can't make it out").
  Live-verified — clean object/scene identification.
- **`describe_image`** — describe/read a local image FILE by path.
- **Endpoints** `POST /vision/screen` and `POST /vision/describe` (accepts a data-URL/base64 frame,
  optional `ocr` flag). Never raise to the client; degrade in character.
- **PWA camera** (`build_pwa.py` overrides): a "what is this / look at this" intent in the browser
  captures **that device's** camera (a phone uses its own back camera via `facingMode:environment`)
  → `/vision/describe` → spoken answer. Screen questions still route to `/chat` (the `read_screen`
  tool sees the PC screen). Rebuilt, wiring verified in `app/web/index.html`.
- **HUD "eye" — VISION panel shows what JARVIS sees:** whenever he looks (webcam or screen, desktop
  voice OR PWA), the backend pushes a thumbnail of the captured frame to the event bus and the
  VISION+GESTURE panel (02) displays that exact frame for ~9s. No continuous camera hold, so it
  never clashes with the `look` tool that owns the webcam during a shot — the panel reflects reality
  instead of a mockup crosshair.
- **`vision` persona block** so JARVIS picks the right eye (screen vs camera vs file) and never
  pretends to have seen something he didn't look at.
- **`prefer` routing** added to the rotator (`vision(..., prefer="gemini")`) — lets a caller put
  the accurate model first while keeping the fast one as the safety net.

New dep: `opencv-python-headless` (free) for the webcam. `mss` + `Pillow` were already present.
Verified: `scripts/vision_smoke.py` **11/11** (imports/tools/router, encoding, graceful degrade,
**live** screen-describe, **live** OCR via the endpoint). App mounts `/vision/*`; messaging smoke
still 59/59 (rotator `prefer` change is backward-compatible).

## Phase 9 — what shipped

- **Installable PWA built from the canonical `JARVIS.html`** (the rule: never redesign — wire the
  provided design). `scripts/build_pwa.py` decodes the `dc-runtime` bundle, recovers the exact HUD
  (reactive arc-reactor orb, ticker, emotion meters, inbox/home panels) and **surgically replaces
  the mockup data path** (`_send`/`_onMic`/`_speak`/scenarios) with real backend calls — every
  animation and pixel preserved. Re-runnable build; React/ReactDOM/Babel + fonts vendored locally
  (offline, no CDN, free). Output in `app/web/`.
- **Backend bridge** (`app/routers/web.py`): `POST /chat` runs the **full agent** (tools + Phase 4
  memory), tagged `channel="pwa_chat"` (cross-channel memory). Returns a `module` hint so the HUD
  side-panel lights up to match the tool that actually ran. Serves `/`, `/manifest.webmanifest`,
  `/sw.js`; static mounted at `/static`.
- **Voice in the browser:** mic captured as 16 kHz WAV via Web Audio → `/voice/stt` → `/chat` →
  reply spoken via `/voice/tts/stream` (Edge Ryan). So PWA voice uses the same agent+memory+tools.
- **Hands-free voice via the proven desktop listener, mirrored into the HUD.** The browser's
  SpeechRecognition wake word proved flaky (gesture rules, mic conflicts, Chrome speech drops), so
  voice is driven by the rock-solid `jarvis_listener.py` (Vosk "wake up jarvis" + Whisper + Edge TTS).
  It POSTs its state to a live **event bus** (`app/routers/events.py`: `WS /events/ws`,
  `POST /events/publish`) and the PWA subscribes — so saying "wake up jarvis" out loud animates the
  orb + transcript + reply on screen in real time, with zero clicking. `scripts/run_pwa.py` now starts
  the backend AND the listener in one command (`--no-voice` to skip). PWA mic button + type box remain
  for manual/phone use. Verified: event publish → WS fan-out, 4/4 events in order.
- **Live HUD ticker:** `GET /ticker` pulls real **crypto (CoinGecko), HN front page, BBC world news,
  and weather** for the user's city (from `MY_PROFILE.md`) — all free/no-key, 60s server cache; the UI
  refreshes it every 60s. Replaces the mocked ticker.
- **PWA bits:** manifest (standalone, theme), service worker (network-first shell so rebuilds show up,
  cache-first immutable assets, never caches API; per-build versioned cache),
  generated arc-reactor icons (192/512/maskable), iOS meta. Wake label fixed to "WAKE UP JARVIS",
  greeting in JARVIS persona ("sir").
- **Reach:** `scripts/run_pwa.py` starts uvicorn + a free **Cloudflare Tunnel** (auto-downloads
  `cloudflared`), printing an HTTPS `trycloudflare.com` URL so the phone can use the mic + install
  the app. `--local` for PC-only. (User-launched; nothing is exposed until you run it.)
- **Hardening:** runner now **refuses to fabricate after a nudge** — if it still claims an action
  with no tool call (a rate-limited fabrication), it tells the truth instead. Protects every channel.
- **Leaked-tool-call recovery:** weaker fallback models sometimes print a tool call as plain-text
  JSON (`{"type":"function","name":"web_search",...}`) instead of a real tool_call — JARVIS used to
  speak that raw blob and never run the tool (so "no current data"). The runner now detects that
  shape (and the legacy `<function=…>` form), **executes the real tool**, and speaks the result.
  Verified: live web_search now returns current data (SpaceX Nasdaq debut, live BTC price).

Verified live: `/chat` recalled identity from `MY_PROFILE.md` ("you're building Stacy…"), used
working memory ("Ross AI is the legal-tech one"), set + listed + cancelled real timers, module hints
correct (inbox/feeds/memory), all shell/static endpoints 200, patched component passes `node --check`.

### Phase 9 — phone UI (`JARVIS-Mobile.html`)

- **Dedicated mobile shell, PC untouched.** `build_pwa.py` now also builds Aditya's phone design
  (`JARVIS-Mobile.html`) into a SEPARATE `app/web/mobile.html` (+ its own `dc-runtime-mobile.js` +
  fonts) — it never writes `index.html`/`sw.js`/`manifest`, and the build **asserts the desktop
  shell is byte-for-byte unchanged**. `web.py`'s `GET /` serves `mobile.html` to phones by
  User-Agent and the **identical** `index.html` to desktop (`?ui=mobile`/`?ui=desktop` force either).
  Hard constraint honoured: *on PC nothing changes — only mobile.*
- **The in-app status bar (clock + battery + 5G + signal) is stripped** — the phone's own OS bar
  already shows it. Everything else of the design (arc-reactor orb, header, chips, HOME/AGENTS/
  COMMS/VISION/MEMORY cards, bottom bar, full-screen call overlay) is preserved.
- **Same backend as the HUD** (the mockup scenarios are replaced): text → `/chat`, mic →
  `/voice/stt`, replies spoken via `/voice/tts/stream`, **live PC mirror** over `/events/ws`
  (when you talk to the PC the phone animates + captions), camera "what is this?" →
  `/vision/describe`, COMMS → `/messaging/inbox`, MEMORY GRAPH → `/memory/graph`, and the
  **incoming-call overlay is real** — driven by Phase-8 `call` events, ANSWER/DECLINE → `/calls/command`.
  Persona fixed (JARVIS, "sir", "WAKE UP JARVIS", never Friday); greeting is visual-only so the
  phone never talks over the PC.
- **Smoke 28/28** (`scripts/mobile_smoke.py`): PC shell unchanged, status bar gone, all 7 endpoints
  wired, overlay/badges bound, patched dc-script passes `node --check`, and routing verified
  (iPhone/Android → mobile.html, desktop → identical index.html, `?ui=` overrides).

## Phase 4 — what shipped

- **3-tier memory + knowledge graph** (`app/services/memory/`), all free/local/persistent:
  - **Tier 1 — Working memory:** live conversation (listener `history`, unchanged).
  - **Tier 2 — Episodic (FAISS):** every turn + explicit `remember` + day-summaries vectorized
    with **local embeddings** (`all-MiniLM-L6-v2`, 384-dim — free, offline, no key, no rate
    limit). `IndexIDMap(IndexFlatIP)` over normalized vectors (cosine), SQLite metadata
    (kind/role/channel/timestamp/mood/entities). FAISS file is a **rebuildable cache** — if it's
    missing/corrupt it re-encodes from SQLite; if embeddings can't load, recall degrades to
    lexical search (never hard-fails). `episodic.py`.
  - **Tier 3 — Semantic facts:** durable key/value (`user.full_name`, `prefs.coffee_order`,
    `contacts.<name>.*`, `routines.*`) in `memory.db`, **injected into every system prompt** so
    JARVIS simply *knows* them without a lookup. `semantic.py`.
  - **Knowledge graph:** `entities` + `relations` triples in `memory.db` for structured
    "tell me about X / who is Y". `graph.py`.
  - **Nightly consolidator (03:00) + on-demand:** one free LLM call distills the last 24 h of
    turns into a day summary (→ episodic) + durable facts (→ semantic) + triples (→ graph);
    graceful, idempotent, retries next cycle on failure. `consolidator.py`.
  - **Per-channel context:** every turn tagged with a channel (`pc_voice` now; `telegram` /
    `whatsapp` / `email` ready for later phases).
- **Agent tools:** `remember(fact, key?)` and `recall(query, since_hours?)` — explicit store/lookup
  on top of the always-on passive recall. `app/tools/memory.py`.
- **Persona:** additive MEMORY prompt block — treat recalled items as genuine recollection, store
  durable facts quietly, recall before guessing, never fabricate a memory. `config.py`.
- **Wiring:** runner injects `WHAT YOU KNOW ABOUT HIM` + `POSSIBLY RELEVANT MEMORIES` each turn;
  listener logs every exchange off-thread and starts the nightly job. `GET /admin/memory`,
  `POST /admin/memory/consolidate`.

Verified: `scripts/mem_smoke.py` — 20/20 in an isolated temp store (vector recall, facts,
graph, tool paths, prompt injection, **restart persistence**, real consolidator distillation,
runner injection).

## Phase 6 — what shipped

- **Tool registry** (`app/tools/`) — `@tool` decorator, auto-discovery, OpenAI schema export,
  robust arg-cleaning (drops hallucinated/extra args, handles null args).
- **AgentRunner** (`app/services/agent/runner.py`) — native function-calling loop over the
  rotator; **hallucination guard** (forces a real tool call if JARVIS claims an action with no
  tool — kills the "coffee is brewing" fakery); malformed-call recovery; multi-step chaining;
  live voice narration between tools. Current date/time injected each turn.
- **Rotator** — `chat_complete(messages, tools, tool_choice)` with provider/key rotation +
  skip-provider-on-malformed.
- **19 tools, all free/local/complete:** web_search (Tavily), get_weather (open-meteo),
  open_app, open_url, play_youtube (yt-dlp), system_status, take_screenshot, media_control,
  set_volume, set_timer, set_reminder, list_reminders, cancel_reminder, note_write,
  note_search, note_today, save_routine, run_routine, list_routines.
- **Persistent scheduler** (`scheduler.py`) — timers AND reminders registered at the
  **Windows OS level via Task Scheduler** (`scripts/alarm_fire.py`), so they fire even when
  JARVIS is closed: real toast + looping alarm sound (verified). When JARVIS is running he
  also speaks the alert; cancelling removes the OS task. No admin needed. SQLite-backed,
  natural when-parser ("in 5 min", "at 8pm", "tomorrow 9am") that reports the clock time.
  **Voice control:** cancel by description ("cancel the gym reminder" / "cancel all"),
  "turn off / stop the alarm" (silences ringing + clears pending), and "snooze N minutes".
  Firing is **non-blocking** (PowerShell off-thread, 1 s tick) — verified multiple timers
  AND reminders all fire on time. Replies always state the clock time (safety net).

### Conversation UX (added during Phase 6 hardening)

- **Wake once, then open conversation** — after "wake up jarvis", JARVIS keeps taking turns
  without re-waking. He returns to wake-word watch only when the user **dismisses** him
  (goodbye / that's all / go to sleep / mute — understood by meaning via a hidden `<SLEEP>`
  signal, not hardcoded phrases) or after a long idle (`CONVO_IDLE_S`, 120 s).
- **Barge-in keeps the interrupting words** — talking over JARVIS stops him AND captures
  what you said as the next command (no lost utterance). Energy-gated so it won't self-trigger.
- **No fabricated actions** — if a tool call malforms, the rotator retries across Groq models
  (8b-instant, llama-4-scout); if it still can't run, JARVIS says so honestly instead of
  pretending (killed the "alarm is set" / "coffee is brewing" fakery for good).
- **Anti-fabrication persona block** added (composable, Phase 6).

Auto-verified: single-tool, multi-step chains (note+weather), timer states clock time +
persists + fires toast, routines save/run, time/date answered directly. Live voice test pending.

### Deferred from Phase 6 (depend on later phases — not stubbed)

- Messaging tools (send/read WhatsApp/Telegram/Instagram/email) → **Phase 7**.
- Vision tools (describe_image / read_screen) → **Phase 2**.
- Calendar (Google/Outlook OAuth), Notion → later (needs OAuth setup).
- Smart-home / **real coffee** (smart plug control) → **Phase 10.E**.
- `run_python` sandbox → deferred (security surface; build with proper sandboxing later).

## Deferred (and why)

_(see per-phase deferrals above)_

## Phase 1 — what shipped

- **Canonical JARVIS persona** (composable prompt blocks, final core) — addresses "sir",
  dry British wit, no LLM-disclaimers. `config.py`.
- **Streaming LLM** — Groq chat, 6-key rotation, Phase-3-extensible. `app/services/llm.py`.
- **STT** — Groq Whisper primary + local faster-whisper offline fallback. `voice/stt.py`.
- **TTS** — Edge-TTS `en-GB-RyanNeural` (free) + ElevenLabs optional upgrade. `voice/tts.py`.
- **VAD** — silero (end-of-speech + barge-in). `voice/vad.py`.
- **Wake word** — **"wake up jarvis"** via grammar-constrained Vosk (free, offline, no
  account, no training). **Verified live**: every "wake up jarvis" matched, other speech →
  [unk]. Two-stage (wake → greet → listen); one-breath traded for reliability. openWakeWord
  `hey_jarvis` available as alt (`WAKE_ENGINE=oww`). `voice/wake_vosk.py`.
- **Endpoints** — `POST /voice/stt`, `POST /voice/tts/stream`, `WS /voice/converse`
  (full-duplex, barge-in). `app/routers/voice.py`.
- **Headless always-on listener** — `scripts/jarvis_listener.py` (the main use surface).
  Streaming TTS uses a **prefetch pipeline**: sentence N+1's audio is synthesized while
  sentence N plays (verified: 0.00s gap after the first sentence).

Auto-verified: persona reply ("I am JARVIS"), TTS+MP3 decode, TTS→STT roundtrip exact,
VAD probs, wake-word load, `/health`, `/voice/tts/stream` (200, audio/mpeg).
Pending Aditya: live mic loop (wake → speak → barge-in) — needs a microphone.

Runtime note: currently runs on the proven `..\JARVIS\.venv` (all deps installed there)
to avoid a multi-GB duplicate. A dedicated venv is a one-liner from `requirements.txt`
whenever wanted — no code change.

## Notes

- Clean rebuild of `..\JARVIS`. Reuse only the solid patterns (Groq rotation, FAISS),
  rebuilt to final quality. Do not port the rough parts.
