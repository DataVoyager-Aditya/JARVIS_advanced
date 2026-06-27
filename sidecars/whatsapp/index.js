/*
 * JARVIS — WhatsApp sidecar (Phase 7).
 *
 * Runs whatsapp-web.js (the engine behind WhatsApp Web) locally. Pair once by scanning the
 * QR printed in this terminal; the session is then saved under .wwebjs_auth/ (LocalAuth) and
 * reused on every later start — no re-scan.
 *
 * It exposes a tiny LOCAL HTTP API the JARVIS Python backend talks to:
 *     GET  /status            -> { ready, state, qr?, me? }
 *     GET  /inbox?limit=15     -> { messages: [{ chat_id, name, body, ts, unread, fromMe }] }
 *     POST /send  {to,message} -> { ok, to } | { error }
 * and PUSHES every incoming message to the backend webhook so JARVIS can announce it.
 *
 * Everything is free: no API, no fees. Bound to 127.0.0.1 and gated by a shared token so
 * only the local JARVIS backend can use it.
 *
 * Config via env (all optional, sane defaults):
 *   PORT                 (3001)
 *   JARVIS_TOKEN         shared secret, must match backend WHATSAPP_WEBHOOK_TOKEN
 *   BACKEND_WEBHOOK_URL  where to push incoming messages
 */

"use strict";

const path = require("path");
const express = require("express");
const qrcode = require("qrcode-terminal");
const { Client, LocalAuth } = require("whatsapp-web.js");

const PORT = parseInt(process.env.PORT || "3001", 10);
const TOKEN = process.env.JARVIS_TOKEN || "jarvis-local-whatsapp";
const WEBHOOK = process.env.BACKEND_WEBHOOK_URL ||
  "http://127.0.0.1:8000/messaging/whatsapp/incoming";

let state = "loading";     // loading | qr | authenticated | ready | disconnected
let lastQr = null;
let meNumber = null;

const client = new Client({
  // Absolute path (based on this file's folder) so the session is found no matter what
  // working directory the process is launched from.
  authStrategy: new LocalAuth({ dataPath: path.join(__dirname, ".wwebjs_auth") }),
  takeoverOnConflict: true,        // if another web session grabs it, take it back
  takeoverTimeoutMs: 10000,
  puppeteer: {
    headless: true,
    protocolTimeout: 120000,       // give slow page loads room (avoids context-destroyed races)
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
  },
});

// Graceful shutdown is CRITICAL for session persistence: a hard kill interrupts Chromium
// mid-write to its IndexedDB (where WhatsApp stores your login), corrupting it so the next
// launch shows the QR again. destroy() closes the browser cleanly and flushes the session.
let _shuttingDown = false;
async function shutdown(sig) {
  if (_shuttingDown) return;
  _shuttingDown = true;
  console.log(`\n[whatsapp] ${sig} — saving session and shutting down cleanly...`);
  try {
    await client.destroy();
  } catch (e) {
    console.warn("[whatsapp] destroy error:", e.message);
  }
  process.exit(0);
}
["SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"].forEach((s) =>
  process.on(s, () => shutdown(s)));

client.on("qr", (qr) => {
  state = "qr";
  lastQr = qr;
  console.log("\n[whatsapp] Scan this QR in WhatsApp → Linked devices:\n");
  qrcode.generate(qr, { small: true });
});

client.on("authenticated", () => { state = "authenticated"; lastQr = null; });
client.on("auth_failure", (m) => { state = "disconnected"; console.error("[whatsapp] auth failure:", m); });
let _chatsWarm = false;
client.on("ready", async () => {
  state = "ready";
  lastQr = null;
  meNumber = client.info && client.info.wid ? client.info.wid.user : null;
  console.log(`[whatsapp] ready as +${meNumber}`);
  // Warm the chat cache so the FIRST /inbox isn't a slow cold call (a big account's initial
  // getChats() can take 20-40s; after this it's instant).
  try {
    console.log("[whatsapp] warming chat + contact cache ...");
    await client.getChats();
    try { _contactsCache = await client.getContacts(); _contactsAt = Date.now(); } catch (_) {}
    _chatsWarm = true;
    console.log("[whatsapp] caches warm — name lookups & reads are fast now.");
  } catch (e) {
    console.warn("[whatsapp] chat warm failed (will warm on first read):", e.message);
  }
});
client.on("disconnected", (r) => { state = "disconnected"; console.warn("[whatsapp] disconnected:", r); });

// Push every genuinely-incoming message to the JARVIS backend.
client.on("message", async (msg) => {
  try {
    if (msg.fromMe) return;
    if (msg.isStatus) return;                 // ignore status/broadcast updates
    let name = msg._data && msg._data.notifyName ? msg._data.notifyName : "";
    try {
      const contact = await msg.getContact();
      name = (contact && (contact.pushname || contact.name || contact.number)) || name;
    } catch (_) { /* contact lookup best-effort */ }
    // For group messages, msg.from is the GROUP id and `name` is the individual member. The
    // group's own name lives on the chat — capture it so the backend can mute the group by name
    // (muting "Ashokans '30" can't match a member's name otherwise).
    const isGroup = /@g\.us$/.test(msg.from || "");
    let group = "";
    if (isGroup) {
      try { const chat = await msg.getChat(); group = (chat && chat.name) || ""; } catch (_) { /* best-effort */ }
    }
    const payload = {
      name: name || msg.from,
      number: (msg.author || msg.from || "").replace(/@c\.us$/, ""),
      body: msg.body || `[${msg.type}]`,
      chat_id: msg.from,
      group: group,
      is_group: isGroup,
      ref: msg.id && msg.id._serialized ? msg.id._serialized : String(msg.timestamp),
    };
    await fetch(WEBHOOK, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-JARVIS-Token": TOKEN },
      body: JSON.stringify(payload),
    }).catch((e) => console.warn("[whatsapp] webhook push failed:", e.message));
  } catch (e) {
    console.warn("[whatsapp] message handler error:", e.message);
  }
});

// Initialize with auto-retry. "Execution context was destroyed" and similar Puppeteer/
// ProtocolError hiccups during startup are almost always transient (WhatsApp Web reloads the
// page mid-inject) and clear on a retry — so we back off and try again instead of giving up.
let _initAttempt = 0;
const _MAX_INIT_ATTEMPTS = 6;

function startClient() {
  _initAttempt += 1;
  client.initialize().catch(async (e) => {
    console.error(`[whatsapp] init failed (attempt ${_initAttempt}/${_MAX_INIT_ATTEMPTS}): ${e.message}`);
    if (_shuttingDown) return;
    if (_initAttempt >= _MAX_INIT_ATTEMPTS) {
      console.error("[whatsapp] giving up. Fix: close any other WhatsApp sidecar, delete the");
      console.error("           sidecars/whatsapp/.wwebjs_auth folder, and start it again.");
      return;
    }
    try { await client.destroy(); } catch (_) { /* half-initialized; ignore */ }
    const delay = Math.min(5000 * _initAttempt, 20000);
    console.log(`[whatsapp] retrying in ${delay / 1000}s ...`);
    setTimeout(startClient, delay);
  });
}
startClient();

// --- resolve a friendly "to" (name / number / chatId) to a real WhatsApp chat id ---
// SCORED matching: exact name > starts-with > all-words-present > (longer) substring. We pick
// the BEST match across saved contacts AND chats (so groups work too) instead of the first
// loose substring hit — that's what caused both "couldn't find" and "sent to the wrong person".
// Resolving a name to a chat id means scanning ALL contacts + chats to score-match — cold that
// is 20-40s on a big account. Cache the result per name (names->ids are stable within a session)
// and cache the contacts list briefly, so the first op on a contact pays it once and every later
// send/read/reply to them is instant.
const _resolveCache = new Map();          // name(lower) -> chatId
let _contactsCache = null, _contactsAt = 0;
const _CONTACTS_TTL = 5 * 60 * 1000;      // 5 min
async function _getContactsCached() {
  const now = Date.now();
  if (_contactsCache && now - _contactsAt < _CONTACTS_TTL) return _contactsCache;
  _contactsCache = await client.getContacts();
  _contactsAt = now;
  return _contactsCache;
}

async function resolveChatId(to) {
  if (!to) throw new Error("no recipient");
  if (/@c\.us$/.test(to) || /@g\.us$/.test(to)) return to;
  const cacheKey = to.toLowerCase().trim();
  if (_resolveCache.has(cacheKey)) return _resolveCache.get(cacheKey);
  const digits = to.replace(/[^\d]/g, "");
  if (digits.length >= 8 && /^[\d +]+$/.test(to)) {
    const numberId = await client.getNumberId(digits);
    const id = numberId ? numberId._serialized : `${digits}@c.us`;
    _resolveCache.set(cacheKey, id);
    return id;
  }
  const q = to.toLowerCase().trim();
  const qWords = q.split(/\s+/).filter(Boolean);
  const score = (name) => {
    if (!name) return 0;
    const n = String(name).toLowerCase().trim();
    if (n === q) return 100;                                         // exact
    if (n.startsWith(q)) return 80;                                  // starts with
    if (qWords.length > 1 && qWords.every((w) => n.includes(w))) return 60;  // all words present
    if (q.length >= 4 && n.includes(q)) return 40;                   // substring (only longer queries)
    return 0;
  };

  let best = null, bestScore = 0, bestName = "";
  const consider = (id, name) => {
    const s = score(name);
    if (s > bestScore) { bestScore = s; best = id; bestName = name; }
  };

  const contacts = await _getContactsCached();
  for (const c of contacts) {
    if (c.isGroup || !c.id || !c.id._serialized) continue;
    consider(c.id._serialized, c.name);
    consider(c.id._serialized, c.pushname);
  }
  const chats = await client.getChats();           // includes groups + non-contact chats (warm)
  for (const ch of chats) {
    if (ch.id && ch.id._serialized) consider(ch.id._serialized, ch.name);
  }

  if (best && bestScore >= 40) {
    console.log(`[whatsapp] resolved "${to}" -> "${bestName}" (score ${bestScore})`);
    _resolveCache.set(cacheKey, best);
    return best;
  }
  throw new Error(`no contact matching "${to}"`);
}

// --- HTTP API (local only, token-gated) ---
const app = express();
app.use(express.json());
app.use((req, res, next) => {
  if ((req.headers["x-jarvis-token"] || "") !== TOKEN) {
    return res.status(401).json({ error: "bad token" });
  }
  next();
});

app.get("/status", (req, res) => {
  res.json({ ready: state === "ready", state, qr: lastQr, me: meNumber, warm: _chatsWarm });
});

app.get("/inbox", async (req, res) => {
  if (state !== "ready") return res.json({ messages: [], state });
  const limit = Math.min(parseInt(req.query.limit || "15", 10), 40);
  try {
    const chats = await client.getChats();
    chats.sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
    const out = [];
    for (const chat of chats.slice(0, limit)) {
      const lm = chat.lastMessage;
      out.push({
        chat_id: chat.id._serialized,
        name: chat.name || (chat.id && chat.id.user) || "",
        body: lm ? (lm.body || `[${lm.type}]`) : "",
        ts: (chat.timestamp || (lm && lm.timestamp) || 0) * 1000,
        unread: chat.unreadCount || 0,
        fromMe: lm ? !!lm.fromMe : false,
      });
    }
    res.json({ messages: out });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post("/send", async (req, res) => {
  if (state !== "ready") return res.status(409).json({ error: "whatsapp not ready", state });
  const { to, message } = req.body || {};
  if (!to || !message) return res.status(400).json({ error: "need {to, message}" });
  try {
    const chatId = await resolveChatId(String(to));
    await client.sendMessage(chatId, String(message));
    res.json({ ok: true, to: chatId });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Read a specific conversation's recent messages (by contact name / number / chat id).
app.get("/chat", async (req, res) => {
  if (state !== "ready") return res.status(409).json({ error: "whatsapp not ready", state });
  const who = req.query.id || req.query.to;
  if (!who) return res.status(400).json({ error: "need ?id=" });
  const limit = Math.min(parseInt(req.query.limit || "15", 10), 50);
  try {
    const chatId = await resolveChatId(String(who));
    const chat = await client.getChatById(chatId);
    const msgs = await chat.fetchMessages({ limit });
    res.json({
      chat_id: chatId,
      name: chat.name || "",
      messages: msgs.map((m) => ({
        from: m.fromMe ? "me" : (m._data && m._data.notifyName) || (m.author || m.from || "").replace(/@c\.us$/, ""),
        body: m.body || `[${m.type}]`,
        ts: (m.timestamp || 0) * 1000,
        fromMe: !!m.fromMe,
      })),
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Delete the most recent message the boss SENT in a chat. everyone=true revokes it for
// everyone (unsend); false deletes only on this side.
app.post("/delete", async (req, res) => {
  if (state !== "ready") return res.status(409).json({ error: "whatsapp not ready", state });
  const { to, everyone } = req.body || {};
  if (!to) return res.status(400).json({ error: "need {to}" });
  try {
    const chatId = await resolveChatId(String(to));
    const chat = await client.getChatById(chatId);
    const msgs = await chat.fetchMessages({ limit: 25 });
    let target = null;
    for (let i = msgs.length - 1; i >= 0; i--) {       // newest first -> most recent of mine
      if (msgs[i].fromMe) { target = msgs[i]; break; }
    }
    if (!target) return res.status(404).json({ error: "no recent message from you in this chat" });
    await target.delete(!!everyone);
    res.json({ ok: true, scope: everyone ? "everyone" : "me", body: target.body || "" });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Mark a conversation as read (clears its unread badge).
app.post("/read", async (req, res) => {
  if (state !== "ready") return res.status(409).json({ error: "whatsapp not ready", state });
  const { to } = req.body || {};
  if (!to) return res.status(400).json({ error: "need {to}" });
  try {
    const chatId = await resolveChatId(String(to));
    const chat = await client.getChatById(chatId);
    await chat.sendSeen();
    res.json({ ok: true, to: chatId });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.listen(PORT, "127.0.0.1", () => {
  console.log(`[whatsapp] sidecar HTTP on http://127.0.0.1:${PORT} (webhook -> ${WEBHOOK})`);
});
