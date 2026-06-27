# JARVIS phone companion (Phase 8 — Calls)

The companion is the bridge between your Android phone's call line and JARVIS. It is **100 %
free, no telephony fees, no SIP, no carrier change.** It does three things:

1. **Tells JARVIS about calls** — POSTs an event when the phone rings (`/calls/incoming`), when
   you miss one (`/calls/missed`), and when a ring ends (`/calls/ended`).
2. **Obeys call commands** — long-polls `/calls/commands` and executes what JARVIS queued when
   you said "decline it" / "silence it" / "answer it" (or tapped the PWA card).
3. **Auto-handles whitelisted callers** (Phase 8.5) — pulls your rules from `/calls/rules` and,
   when a matching contact calls, auto-texts them ("can't talk, call you back"), auto-answers on
   speaker, or silently declines — all on the phone itself.

Pick **one** of two setup paths. They talk to the exact same backend, so you can start on
Macrodroid today and move to the app later with zero backend changes.

| | **A. Macrodroid recipe** | **B. Kotlin app** (`app/`) |
|---|---|---|
| Setup | Install Macrodroid (free), import a profile, grant access. **~5 min, no coding.** | Build the APK in Android Studio, sideload, grant permissions. One-time build. |
| Announce incoming | ✅ | ✅ |
| Missed-call query | ✅ | ✅ (reads `CallLog`) |
| Decline / silence | ⚠️ best-effort (Macrodroid's call actions vary by Android version) | ✅ clean (`TelecomManager.endCall()`) |
| Answer on speaker | ⚠️ flaky | ✅ (`acceptRingingCall()`, needs the permission) |
| Auto-text a caller | ✅ (SMS action) | ✅ (`SmsManager`) |
| Always-on | ✅ (Macrodroid service) | ✅ (foreground service) |

**Both paths need:** your PC's backend reachable from the phone, and the shared token. When you
run JARVIS over the Cloudflare tunnel (`scripts/run_pwa.py`), use that HTTPS URL; on the same
Wi-Fi you can use `http://<PC-LAN-IP>:8000`. The token is `CALLS_WEBHOOK_TOKEN` from your `.env`
(default `jarvis-local-calls` — **change it** to something private before exposing the tunnel).

---

## Path A — Macrodroid recipe (zero code)

Full step-by-step in **[macrodroid-recipe.md](macrodroid-recipe.md)**. In short you create 3
macros in the free Macrodroid app:

- **Ring → announce:** Trigger *Call → Phone Ringing*; Action *HTTP Request* `POST {BACKEND}/calls/incoming`
  with header `x-jarvis-token: {TOKEN}` and JSON body `{"number":"[call_number]","name":"[call_name]"}`.
- **Missed → notify:** Trigger *Call → Call Missed*; Action *HTTP Request* `POST {BACKEND}/calls/missed`
  with the same header and body.
- **Poll for commands:** Trigger *Regular Interval → 3 s* (only while the screen call is active,
  or always if you prefer); Action *HTTP Request GET* `{BACKEND}/calls/commands`; then a
  *Macrodroid → Parse the response* + conditional actions: if it contains `"decline"` → *Call →
  Answer/Endcall (End)*, `"answer"` → *Answer*, `"silence"` → *Volume → Ringer mute*.
- **(8.5) Auto-text:** Trigger *Call → Phone Ringing* with a constraint *Caller is [contact]*;
  Action *End Call* then *Send SMS* "Can't talk right now, I'll call you back."

Macrodroid stores the BACKEND + TOKEN as **macro variables** so you set them once. The recipe
doc has the exact field values to type.

---

## Path B — Kotlin app (advanced, this folder)

A small, single-purpose Android app. Open `companion-android/` in **Android Studio**, let it
sync Gradle, plug your phone in (USB debugging on), and **Run** — or *Build → Build APK* and
sideload the APK. First launch:

1. Enter your **Backend URL** and **Token**, tap **Save & Start**.
2. Grant the permissions it asks for: Phone, Call Log, Contacts, SMS, Notifications. For
   answer/decline to work, also grant **"Answer phone calls"** (the app links you straight to it).
3. Disable battery optimisation for the app when prompted (so Android won't kill the service).

It then runs a persistent foreground service ("JARVIS is watching your calls"). That's it —
ring your phone and JARVIS announces it on the PC.

> **Note:** the source here is final-quality and self-contained, but it must be **compiled on
> your machine** (Android Studio + JDK 17) — an APK can't be pre-built/signed for you here. The
> Macrodroid path needs no build, which is why it's the recommended starting point.

See **[app/SETUP.md](app/SETUP.md)** for the build walkthrough and permission notes.

---

## What calls cannot do for free (and why)

- **JARVIS holding a spoken conversation on the call** (greeting + voicemail + full back-and-forth)
  is **Phase 8.5-conversational**, built separately via the **PC-as-Bluetooth-handset** path —
  it does NOT use the blocked Android call-audio APIs. See the project notes; it needs your
  phone paired to the PC and is verified with your hardware in the loop.
- **Placing outbound calls / dialing** — JARVIS won't dial for you (no free, reliable programmatic
  dialing that respects carrier rules). He'll offer to message the person instead.
