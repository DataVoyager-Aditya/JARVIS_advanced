# JARVIS — Project Constitution (auto-loaded every session)

This is the **JARVIS_advanced** build. It is a clean, disciplined rebuild of the older
`c:\Users\Lenovo\Desktop\JARVIS` project (which was built with a messy "few new + few
polish" loop and left half-baked features, in-memory hacks, and persona breaks).

**The full, binding rules live in [.claude/RULES.md](.claude/RULES.md). Read them.**
The hard constraints, summarized — never violate these:

1. **Name is JARVIS. Never "Friday". JARVIS is male — he/him, never she/her.** PLANNER.md
   says "Friday" (and "she") everywhere — mentally substitute **JARVIS / he** in all of it.
   Ignore the Friday persona/renaming entirely.

2. **One phase at a time, in order.** Do not begin a phase until the previous one is
   built, verified, and recorded in [STATUS.md](STATUS.md). No skipping ahead.

3. **Every feature you build is the FINAL, most-advanced version — first time.**
   No "we'll polish it later." No placeholders, no in-memory stubs that "we'll persist
   later," no TODO_POLISH. If you cannot build a feature fully and for free right now,
   **defer the whole feature** — do not ship a rough version. We never go back to
   improve a feature that's already shipped.

4. **100% free, forever.** Free-tier APIs (no credit card) or multi-key rotation only.
   Nothing that can ever charge money. If the only good option costs money, find a free
   one or defer.

5. **Voice = free, JARVIS-like.** Primary free engine is **Edge-TTS** (Microsoft,
   unlimited, no key) with voice **`en-GB-RyanNeural`** (refined British male — the
   JARVIS sound; alt `en-GB-ThomasNeural`). Any paid/higher-quality engine (e.g.
   ElevenLabs free tier with rotated keys) may sit *on top* as an optional upgrade, but
   Edge-TTS must always work as the free fallback so voice never breaks or costs money.

6. **Production-grade or not at all.** Real persistence (survives restart), real error
   handling, real OS integration, verified with a smoke test before "done."

When in doubt, re-read [.claude/RULES.md](.claude/RULES.md).
