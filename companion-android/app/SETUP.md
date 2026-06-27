# Building the JARVIS companion app

The advanced (Path B) companion. Final-quality source — you compile it once on your machine.

## Prerequisites
- **Android Studio** (free) with **JDK 17** (bundled).
- Your phone with **Developer options → USB debugging** on (to Run directly), or you'll build an
  APK and sideload it.

## Build & install
1. Android Studio → **Open** → select the `companion-android/` folder. Let Gradle sync (it
   downloads the Android Gradle Plugin + Kotlin the first time).
2. Plug in your phone, pick it in the device dropdown, press **Run ▶** — OR **Build → Build
   Bundle(s)/APK(s) → Build APK(s)**, then copy `app/build/outputs/apk/debug/app-debug.apk` to
   the phone and tap it to install (allow "install from unknown sources").

## First launch
1. Enter your **Backend URL** (the `trycloudflare.com` URL from `scripts/run_pwa.py`, or
   `http://<PC-LAN-IP>:8000` on the same Wi-Fi) and the **Token** (`CALLS_WEBHOOK_TOKEN` in `.env`).
2. Tap **Test backend connection** — should say "reachable ✓".
3. Tap **Save & Start**. Grant the permission prompts: Phone, Call Log, Contacts, SMS,
   Notifications.
4. **Answer phone calls permission:** for decline/answer to work, Android requires the
   "Answer phone calls" capability. If a prompt doesn't appear, go to **Settings → Apps → JARVIS
   Companion → Permissions → Phone** and ensure it's allowed. (On some OEMs this is also gated
   behind making no default-dialer change — the app only needs the permission, not to be the dialer.)
5. Tap **Disable battery optimisation** so Android keeps the service alive.

A persistent "JARVIS is watching your calls" notification means it's live.

## What works
- Ring → JARVIS announces it on the PC + the PWA shows the call card.
- You say "wake up JARVIS, decline" or tap the card → the app declines/answers/silences via
  `TelecomManager`.
- "Any missed calls?" → JARVIS reads them.
- Phase 8.5 rules you set by voice ("auto-text Mom that I'll call back", "auto-answer my
  co-founder on speaker") are pulled by the app and enforced on the next matching call.

## What it deliberately does NOT do
- It never touches the call's **voice audio** — Android blocks that for third-party apps. JARVIS
  speaking *on* the call (greeting/voicemail/conversation) is the separate **PC-as-Bluetooth-
  handset** path, which routes call audio to your PC over the Hands-Free profile instead.
- No outbound dialing.

## Privacy
Everything stays between your phone and your own JARVIS backend (your PC / your tunnel). The
token gates the endpoints. No third-party servers, no analytics, nothing leaves your control.
