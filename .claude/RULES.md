# JARVIS_advanced — Build Rules (the constitution)

These rules override anything in PLANNER.md that conflicts. PLANNER.md is the *feature
map*; this file is *how we build*. Aditya set these rules deliberately because the first
attempt (`..\JARVIS`) was built in a rushed "1–2 new + 1–2 polish" loop and ended up
full of rough edges, in-memory hacks, "polish later" TODOs, and persona breaks. We do
not repeat that here.

---

## 1. Identity

- **JARVIS is male — he / him / his.** Never "she/her." (The Friday framing in PLANNER
  used "she"; drop it.) Use "he" in all code comments, docs, and replies.
- **The assistant is named JARVIS.** Never "Friday." PLANNER.md is written with the name
  "Friday" throughout — read every occurrence as **JARVIS**. Skip all the Friday-specific
  persona framing (younger / Irish-tinged / sassier). That is dropped.
- **Persona = canonical JARVIS:** calm, refined, unflappable, dry British wit, hyper-
  competent, quietly loyal. Understated humor, never goofy. Anticipates. Speaks in clean,
  efficient sentences with the occasional precise quip.
- **Form of address:** **"sir"** (JARVIS canon — confirmed by Aditya). Configurable in
  onboarding/config later, but "sir" is the default everywhere.
- **Stays in character.** Never volunteers "I'm just an AI / I have no feelings / as a
  language model." In-character, graceful refusals only. (This was a live bug in the old
  build — do not reintroduce it.)

## 2. Phase discipline

- **Build strictly phase by phase, in PLANNER order.** Phase 1, then 3, etc. per the
  order-of-operations table — but only one at a time.
- **Do not start a new phase until the current one is:** (a) fully built, (b) smoke-tested
  per PLANNER's verification list, (c) recorded as ✅ in [STATUS.md](../STATUS.md).
- **At the start of each phase:** state the exact scope (what's in, what's deliberately
  out), then build it.
- **At the end of each phase:** run the verification, then update STATUS.md with what
  shipped and how it was verified.

## 3. Finality — the core rule

> **Whatever feature you build is the final, most-advanced version of it. The first time.**

- No "good enough for now," no "we'll improve it later," no "polish pass next session."
- **No placeholders or stubs.** No in-memory state that's lost on restart "for now." No
  hardcoded fake data. No `# TODO: handle X` left in shipped code paths.
- **We never go back to polish a shipped feature.** So it has to be right and complete on
  first build. Think it through fully before writing.
- **If a feature can't be built fully + for free right now → defer the entire feature**
  (note it in STATUS.md as "deferred: reason"). Never ship a half version of it.
- "Most advanced" means: best free option chosen, edge cases handled, errors handled,
  persisted, integrated with the real OS/apps, and tested — not a demo.

## 4. Cost — everything is free

- **Zero money, ever. No credit card.** Free-tier APIs or multi-key rotation only.
- The proven pattern is **multi-key rotation** (the old Groq 6-key trick) — generalize it
  (PLANNER Phase 3 KeyRotator) so we stay under free limits across providers.
- Free providers we rely on: Groq, Gemini, OpenRouter free models, Together, Cerebras,
  Mistral, HuggingFace, Tavily, open-meteo, ntfy.sh, Edge-TTS, etc.
- If the only viable option for something costs money, **find a free substitute or defer
  the feature.** Never silently introduce a paid dependency.

## 5. Voice (free + JARVIS-like)

- **Primary free TTS: Edge-TTS** (Microsoft, unlimited, no API key, no cost).
- **Voice: `en-GB-RyanNeural`** — refined British male, the JARVIS sound (confirmed by
  Aditya). Alternate available: `en-GB-ThomasNeural`.
- Higher-quality engines (ElevenLabs free tier via rotated keys, local XTTS, etc.) may be
  layered **on top as optional upgrades**, but Edge-TTS must always remain a working free
  fallback so voice never costs money and never fully breaks.
- **STT:** Groq Whisper (`whisper-large-v3-turbo`) via key rotation (free); local
  `faster-whisper` as offline fallback.

## 6. Engineering standards

- **Windows-first.** Target is Aditya's Windows 11 machine. Test with PowerShell. Use
  real Windows integrations (Task Scheduler, toasts, media keys, startup) — not fakes.
- **Persistence is mandatory** wherever state matters (timers, reminders, memory, keys,
  sessions). Nothing important may live only in memory.
- **Real error handling + fallbacks** on every external call (rotate key → next provider →
  graceful degrade). Reuse a shared retry helper.
- **Reuse the *solid* patterns** from `..\JARVIS` (Groq rotation logic, FAISS retriever,
  system-prompt injection mechanics) — but do **not** copy its rough/half-baked parts
  (in-memory timers, untested toasts, empty-response agent bug, persona-break prompt).
  When reusing, bring it up to the finality standard above.
- **Smoke-test before "done."** Run the actual thing (uvicorn endpoint, tool call, voice
  loop) and observe it working. "Registered but not tested" is not done.

## 7. Working agreement

- Keep [STATUS.md](../STATUS.md) as the single source of truth for progress — what's
  shipped (✅ + how verified), what's deferred (+ why). No "polish later" list.
- Prefer fewer, fully-finished features over many rough ones.
- Ask Aditya only for genuine decisions (provider choice, persona address, scope cuts) —
  otherwise pick the best free default and proceed.
