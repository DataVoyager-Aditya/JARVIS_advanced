# Macrodroid recipe — JARVIS calls (zero code)

Set this up once in the free **Macrodroid** app (Play Store) and your phone tells JARVIS about
calls and obeys his call commands — no programming, no APK build. ~5 minutes.

## 0. One-time variables

Macrodroid → **☰ → Variables → +** (create two **String** global variables):

- `JARVIS_URL` = your backend, e.g. `https://something.trycloudflare.com` (the URL
  `scripts/run_pwa.py` prints) or `http://192.168.1.x:8000` on the same Wi-Fi. **No trailing slash.**
- `JARVIS_TOKEN` = your `CALLS_WEBHOOK_TOKEN` from `.env` (default `jarvis-local-calls` — change it!).

Grant Macrodroid these accesses when it asks: **Phone**, **Contacts**, **Notification access**,
**SMS** (for auto-text), and disable battery optimisation for Macrodroid.

> **Headers — add BOTH to every HTTP Request below:**
> - `x-jarvis-token: {lv=JARVIS_TOKEN}`
> - `ngrok-skip-browser-warning: true`  ← only needed on a free **ngrok** URL; without it ngrok
>   may return its "you are about to visit" HTML page instead of reaching JARVIS. (Harmless to
>   include on any URL.)

---

## Macro 1 — Announce an incoming call

- **Trigger:** Call → **Phone Ringing**
- **Action:** Connectivity → **HTTP Request**
  - Method: **POST**
  - URL: `{lv=JARVIS_URL}/calls/incoming`
  - Content type: **application/json**
  - Custom headers: `x-jarvis-token: {lv=JARVIS_TOKEN}`
  - Body:
    ```json
    {"number":"{call_number}","name":"{call_name}","ref":"ring-{call_number}-{system_time}"}
    ```
  > Macrodroid magic text: `{call_number}` and `{call_name}` are filled from the ringing call.

---

## Macro 2 — Report a missed call

- **Trigger:** Call → **Call Missed**
- **Action:** Connectivity → **HTTP Request**
  - **POST** `{lv=JARVIS_URL}/calls/missed`
  - Header `x-jarvis-token: {lv=JARVIS_TOKEN}`, JSON body:
    ```json
    {"number":"{call_number}","name":"{call_name}","ref":"miss-{call_number}-{system_time}"}
    ```

---

## Macro 3 — Obey JARVIS's commands (decline / answer / silence / dial)

- **Trigger:** **Regular Interval → every 3 seconds** *(poll ALWAYS — not only while ringing — so
  outbound "call X" works too)*
- **Action 1:** HTTP Request **GET** `{lv=JARVIS_URL}/calls/commands`
  - Header `x-jarvis-token: {lv=JARVIS_TOKEN}`
  - "Save response to variable" → a string var `CMD_RESP`
- **Action 2 (decline):** *If* `CMD_RESP` **contains** `"decline"` → Call → **End Call**
- **Action 3 (answer):** *Else If* `CMD_RESP` **contains** `"answer"` → Call → **Answer Call**
- **Action 4 (silence):** *Else If* `CMD_RESP` **contains** `"silence"` → Volume → **Silent /
  Ringer mute**
- **Action 5 (dial):** *Else If* `CMD_RESP` **contains** `"dial"`:
  1. Variable → set string `DIAL_NUM` = `CMD_RESP`, then **Variable: Search/Replace (regex)** on
     `DIAL_NUM` with pattern `.*"number":"([^"]+)".*` → replace `$1` (leaves just the number).
  2. Call → **Make Call** to `{lv=DIAL_NUM}` (this places the call on your phone).
- **End If**

> **Decline / Answer:** Macrodroid's call actions use the Accessibility service — enable
> "MacroDroid Accessibility" in Android settings. On Android 12+ they can be inconsistent across
> OEMs; that's the limitation the Kotlin app (`TelecomManager`) solves cleanly.
>
> **Make Call** needs Macrodroid's **Phone** permission. JARVIS only *dials* — you talk on the
> phone as normal (he can't speak on the call).

---

## Macro 4 (Phase 8.5) — Auto-text a specific caller

One macro **per contact** you want auto-handled. The per-caller match is set **inside the
trigger** (Macrodroid has no separate "caller is X" constraint):

- **Trigger:** Call → **Incoming Call**. When you add it, Macrodroid asks **which contacts** it
  applies to — choose **Specific Contact(s)** (or a **Contact Group**) and pick e.g. *Mom*. That's
  what limits this macro to that caller. *(If you used "Phone Ringing" for Macro 1, note that
  "Incoming Call" is the one with the contact selector — use it here.)*
- **Action 1:** Call → **End Call**
- **Action 2:** Messaging → **Send SMS** to `{call_number}`: *"Can't take your call right now —
  I'll call you back shortly."*
- **Action 3 (optional):** HTTP POST `{lv=JARVIS_URL}/calls/missed` so JARVIS logs it too.

> No contact selector on your trigger version? Then keep a general **Incoming Call** trigger and
> add an **If** action at the top: *If `{call_name}` (or `{call_number}`) **equals** "Mom"* →
> End Call + Send SMS, **End If**. Same result, just matched inside the macro.

---

## Test it

1. Make sure JARVIS is running (`scripts/run_pwa.py --whatsapp`) and reachable at `JARVIS_URL`.
2. Have a friend call you (or call from another phone). JARVIS should **announce it on the PC**
   and show the **incoming-call card** in the PWA.
3. Say "**wake up JARVIS, decline the call**" (or tap **DECLINE** on the PWA card). Within ~3 s
   Macrodroid's poll picks up the command and ends the call.
4. Ask JARVIS "**any missed calls?**" — he reads them back.

If announcements don't arrive, check: URL has no trailing slash, the token matches `.env`, and
the phone can actually reach the PC (open `JARVIS_URL/health` in the phone browser).
