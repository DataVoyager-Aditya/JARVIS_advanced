# JARVIS WhatsApp sidecar (Phase 7)

A tiny local Node service that bridges WhatsApp to JARVIS using
[`whatsapp-web.js`](https://wwebjs.dev/) — the same engine as WhatsApp Web. **Free, no API,
no fees.** It pairs once by QR, persists the session, exposes a local HTTP API, and pushes
incoming messages to the JARVIS backend so he can announce them.

## One-time setup

```powershell
cd sidecars/whatsapp
npm install          # downloads whatsapp-web.js + a headless Chromium (~300 MB, once)
node index.js        # a QR code prints in the terminal
```

Open **WhatsApp on your phone → Settings → Linked devices → Link a device**, scan the QR.
After "`[whatsapp] ready as +<number>`", the session is saved under `.wwebjs_auth/` and you
won't need to scan again.

> `scripts/run_pwa.py` can start this for you automatically once `npm install` has been run
> (see `--whatsapp`). You can also just leave `node index.js` running in its own window.

## What it exposes (local only, token-gated)

| Method | Path | Purpose |
|---|---|---|
| GET | `/status` | `{ ready, state, qr?, me? }` |
| GET | `/inbox?limit=15` | recent chats: `{ chat_id, name, body, ts, unread, fromMe }` |
| POST | `/send` `{to, message}` | `to` = contact name, phone number, or `…@c.us` |

Every inbound message is POSTed to `BACKEND_WEBHOOK_URL`
(default `http://127.0.0.1:8000/messaging/whatsapp/incoming`) with the shared token header.

## Config (env, all optional)

| Var | Default | Notes |
|---|---|---|
| `PORT` | `3001` | HTTP port (bound to 127.0.0.1) |
| `JARVIS_TOKEN` | `jarvis-local-whatsapp` | must equal backend `WHATSAPP_WEBHOOK_TOKEN` |
| `BACKEND_WEBHOOK_URL` | `…/messaging/whatsapp/incoming` | where incoming messages are pushed |

## Notes / safety

- Unofficial automation. At personal volume (reading + a few sends a day) this behaves like
  a normal linked device; bulk/automated blasting is what risks a ban — don't do that.
- The session lives in `.wwebjs_auth/` (gitignored). Delete that folder to unlink/re-pair.
- If WhatsApp updates their web client and the library lags, `npm update whatsapp-web.js`.
