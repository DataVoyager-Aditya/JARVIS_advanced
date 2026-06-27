"""
Build the JARVIS PWA from the canonical design `JARVIS.html` (Phase 9).

`JARVIS.html` is a self-unpacking `dc-runtime` React design — a gorgeous Iron-Man HUD, but
shipped as a MOCKUP (canned `scenarios`). This script derives a real, backend-wired,
installable PWA from it WITHOUT redesigning anything:

  1. Decodes the bundle -> recovers the dc-runtime, the `<x-dc>` markup, the fonts, and the
     component source (`data-dc-script`).
  2. Patches the component: overrides `_send` / `_onMic` / `_speak` / `_runScenario` to call
     the real JARVIS backend (agent + memory + tools + voice) instead of the canned scenarios,
     and fixes the wake label + intro greeting to JARVIS persona. The entire visual design and
     every animation are preserved untouched.
  3. Vendors React / ReactDOM / Babel + the fonts locally (free, offline — no CDN at runtime).
  4. Emits `app/web/` : index.html, static/*, manifest.webmanifest, sw.js, icons.

It then ALSO builds the phone UI from `JARVIS-Mobile.html` into a SEPARATE `app/web/mobile.html`
(+ its own `dc-runtime-mobile.js`), wired to the same backend. web.py serves that to phones by
User-Agent, so the desktop HUD (index.html) is left byte-for-byte identical — see build_mobile().

Re-run any time `JARVIS.html` or `JARVIS-Mobile.html` changes:  python scripts/build_pwa.py
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "JARVIS.html"
MSRC = ROOT / "JARVIS-Mobile.html"          # Aditya's phone design (separate dc bundle)
WEB = ROOT / "app" / "web"
STATIC = WEB / "static"
FONTS = STATIC / "fonts"
VENDOR = STATIC / "vendor"
ICONS = STATIC / "icons"

VENDOR_LIBS = {
    "react.production.min.js": "https://unpkg.com/react@18.3.1/umd/react.production.min.js",
    "react-dom.production.min.js": "https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js",
    "babel.min.js": "https://unpkg.com/@babel/standalone@7.26.4/babel.min.js",
}

# --- the backend-wiring overrides appended to the component class (last def wins) --------- #
OVERRIDES = r"""

  // ===== Phase 9: real JARVIS backend wiring (overrides the mockup scenarios) =====
  _ask = (txt) => {
    this._clearStream(); clearTimeout(this._idleTimer);
    // Phase 2 — camera-vision intent in the BROWSER uses THIS device's camera (so a phone uses
    // its own camera, not the PC's). Screen questions keep going to /chat — the read_screen tool
    // sees the PC screen where the backend runs.
    if (/\b(what(?:'?s| is)?\s+this|what am i (?:holding|showing|looking at)|look at this|can you see this|use (?:the |my )?camera)\b/i.test(txt) && !/screen/i.test(txt)) {
      this._lookCam(txt); return;
    }
    this.setState({ userTranscript: txt, mode: "thinking", activeModule: null });
    fetch(window.JARVIS_API + "/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: txt, session_id: window.JARVIS_SID })
    })
      .then(r => r.json())
      .then(d => { this._speak((d && d.reply) || "I couldn't reach my services just now, sir.", d && d.module); })
      .catch(() => { this._speak("I couldn't reach my services just now, sir."); });
  };

  // Phase 2 — grab a frame from this device's camera and ask the vision model about it.
  // Lights up the VISION panel (visionActive) while it captures, then speaks the answer.
  async _lookCam(question) {
    this.setState({ userTranscript: question, mode: "thinking", visionActive: true });
    let stream = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 1280 } } });
    } catch (e) {
      this.setState({ visionActive: false });
      this._speak("I can't reach your camera, sir — check the browser's camera permission."); return;
    }
    try {
      const video = document.createElement("video");
      video.srcObject = stream; video.muted = true; video.playsInline = true; video.setAttribute("playsinline", "");
      await video.play().catch(() => {});
      await new Promise((r) => setTimeout(r, 500));            // let exposure/white-balance settle
      const w = video.videoWidth || 1280, h = video.videoHeight || 720;
      const canvas = document.createElement("canvas"); canvas.width = w; canvas.height = h;
      canvas.getContext("2d").drawImage(video, 0, 0, w, h);
      const dataUrl = canvas.toDataURL("image/jpeg", 0.72);
      this._visionFrame(dataUrl);                              // show what the camera grabbed
      const r = await fetch(window.JARVIS_API + "/vision/describe", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image: dataUrl, question })
      });
      const d = await r.json();
      this._speak((d && d.ok && d.text) || (d && d.error) || "I couldn't make that out, sir.", "vision");
    } catch (e) {
      this._speak("Something went wrong with the camera, sir.");
    } finally {
      try { stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
      this.setState({ visionActive: false });
    }
  }

  _send = () => {
    const el = this._input; if (!el) return;
    const txt = el.value.trim(); if (!txt) return; el.value = "";
    this._ask(txt);
  };

  _runScenario = (key) => {
    const m = {
      research: "Give me a short brief on the latest in AI today.",
      inbox: "What reminders do I have set?",
      vision: "What's on my screen right now?",
      feeds: "Give me a quick world news brief.",
      identity: "Who has access to you right now?"
    };
    this._ask(m[key] || key);
  };

  _speak(text, module) {
    this.setState({ mode: "speaking", activeModule: module || null });
    this._playTTS(text);
    this._streamText(text, "jarvis", () => {
      this._idleTimer = setTimeout(() => this.setState({ mode: "idle" }), 900);
    });
  }

  async _playTTS(text) {
    try {
      if (this._audio) { try { this._audio.pause(); } catch (e) {} }
      const r = await fetch(window.JARVIS_API + "/voice/tts/stream", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text })
      });
      if (!r.ok) return;
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = new Audio(url); this._audio = a;
      a.onended = () => URL.revokeObjectURL(url);
      a.play().catch(() => {});
    } catch (e) {}
  }

  _onMic = () => {
    if (this.state.mode === "thinking") return;
    if (this._recState && this._recState.recording) { this._stopRec(); return; }
    this._startRec();
  };

  // ===== hands-free wake word: "wake up jarvis" via the browser's free SpeechRecognition =====
  _initWake() {
    if (this._wakeInited) return;
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { console.warn("[jarvis] SpeechRecognition unavailable in this browser — use the mic button."); return; }
    this._wakeInited = true;
    const rec = new SR();
    rec.continuous = true; rec.interimResults = true; rec.lang = window.JARVIS_LANG || "en-US";
    rec.onresult = (e) => this._onWakeResult(e);
    rec.onerror = (ev) => { if (ev.error === "not-allowed" || ev.error === "service-not-allowed") this._wakeOn = false; };
    rec.onend = () => { if (this._wakeOn) { try { rec.start(); } catch (e) {} } };
    this._wake = rec; this._wakeOn = true;
    this._startWake();
    // Some browsers require a user gesture before the first start — retry on any tap.
    document.addEventListener("pointerdown", () => this._startWake());
  }

  _startWake() {
    if (!this._wake || !this._wakeOn) return;
    try { this._wake.start(); } catch (e) { /* already running */ }
  }

  // ===== live voice mirror: the desktop listener drives the HUD over a WebSocket =====
  // The PC listener does the hands-free wake word + STT + reply + speaks aloud; here we just
  // ANIMATE to it (no browser TTS — the PC is already talking).
  _initEvents() {
    if (this._evtInited) return; this._evtInited = true;
    const connect = () => {
      let ws;
      try {
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(proto + "//" + location.host + "/events/ws");
      } catch (e) { setTimeout(connect, 2000); return; }
      this._evtWs = ws;
      ws.onmessage = (m) => { try { this._onVoiceEvent(JSON.parse(m.data)); } catch (e) {} };
      ws.onclose = () => { setTimeout(connect, 2000); };   // survive backend restarts
      ws.onerror = () => { try { ws.close(); } catch (e) {} };
    };
    connect();
  }

  _onVoiceEvent(ev) {
    if (!ev || !ev.type) return;
    if (ev.type === "listening") {
      this._clearStream(); clearTimeout(this._idleTimer); clearTimeout(this._safetyTimer);
      this.setState({ mode: "listening", caption: "", userTranscript: "" });
    } else if (ev.type === "transcript") {
      this._clearStream(); clearTimeout(this._idleTimer); clearTimeout(this._safetyTimer);
      this.setState({ userTranscript: ev.text || "", caption: "", mode: "thinking" });
    } else if (ev.type === "reply") {
      if (ev.text) this._speakVisual(ev.text);   // stays 'speaking' until the desktop sends idle
      else this.setState({ mode: "idle" });
    } else if (ev.type === "idle") {
      // The desktop finished speaking — drop to standby (a following 'listening' will override).
      clearTimeout(this._safetyTimer);
      this._idleTimer = setTimeout(() => this.setState({ mode: "idle" }), 250);
    } else if (ev.type === "notify") {
      // A new WhatsApp/Instagram/email message just arrived — refresh the comms panel now.
      if (this._pullComms) this._pullComms();
    } else if (ev.type === "vision_frame") {
      // JARVIS just looked at something (webcam/screen) — show the actual frame in the VISION
      // panel so the HUD reflects what he sees. (No continuous camera hold → no clash with the
      // backend `look` tool, which owns the webcam during a capture.)
      this._visionFrame(ev.image);
    } else if (ev.type === "mood") {
      // Phase 5 — JARVIS's live emotional read drives the EMOTION panel (register + 4 axes).
      this.setState({ emReal: {
        reg: (ev.label || ev.register || "NEUTRAL"),
        warmth: ev.warmth, play: ev.play, urgency: ev.urgency, focus: ev.focus,
        humor: (ev.humor != null ? ev.humor : 0.4),
      } });
    } else if (ev.type === "call") {
      // Phase 8 — a phone-call event from the Android companion. An incoming ring shows a
      // tappable card (answer/decline/silence -> /calls/command); a cleared/missed event hides it.
      if (ev.kind === "incoming") this._callCard(ev);
      else this._hideCallCard();
    } else if (ev.type === "identity") {
      // Phase 11 — who is using JARVIS right now drives the IDENTITY panel (top-left).
      this._setIdentity(ev);
    }
  }

  // Phase 11 — seed the IDENTITY panel from the backend on load, then live-update on 'identity'.
  _initIdentity() {
    fetch(window.JARVIS_API + "/identity/active")
      .then(r => r.json()).then(d => { if (d) this._setIdentity(d); }).catch(() => {});
  }

  _setIdentity(d) {
    if (!d) return;
    const tier = (d.tier || "owner");
    this.setState({
      identityLine: d.line || "ADITYA · OWNER",
      identityVp: d.biometric || "VOICEPRINT 99.2%",
      identityInitial: d.initial || "A",
      identityStatus: (tier === "stranger") ? "UNVERIFIED" : "VERIFIED",
    });
  }

  // Body-level incoming-call card (same overlay approach as _visionFrame, so a React render
  // never wipes it). Buttons relay the command to the phone via the backend command queue.
  _callCard(ev) {
    let c = this._callEl;
    if (!c || !document.body.contains(c)) {
      c = document.createElement("div");
      c.style.cssText = "position:fixed;z-index:10000;left:50%;top:28px;transform:translateX(-50%);" +
        "min-width:300px;padding:14px 18px;border-radius:10px;background:rgba(6,16,24,.94);" +
        "border:1px solid rgba(53,231,255,.55);box-shadow:0 0 26px rgba(53,231,255,.45);" +
        "font-family:inherit;color:#cdeefb;text-align:center;backdrop-filter:blur(4px);";
      document.body.appendChild(c);
      this._callEl = c;
    }
    const who = (ev.name || ev.number || "Unknown");
    const btn = (label, action, col) =>
      `<button data-act="${action}" style="cursor:pointer;margin:0 5px;padding:7px 14px;border-radius:7px;` +
      `border:1px solid ${col};background:transparent;color:${col};font:inherit;font-size:12px;letter-spacing:.5px;">` +
      `${label}</button>`;
    c.innerHTML =
      '<div style="font-size:11px;letter-spacing:2px;opacity:.7;">INCOMING CALL</div>' +
      '<div style="font-size:19px;margin:6px 0 12px;color:#fff;">' + who + '</div>' +
      btn("ANSWER", "answer", "#4ade80") + btn("DECLINE", "decline", "#ff5d6c") +
      btn("SILENCE", "silence", "#9bb6c4");
    c.querySelectorAll("button").forEach((b) => {
      b.onclick = () => {
        fetch(window.JARVIS_API + "/calls/command", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: b.getAttribute("data-act") }),
        }).catch(() => {});
        this._hideCallCard();
      };
    });
    c.style.display = "block";
    clearTimeout(this._callTimer);
    this._callTimer = setTimeout(() => this._hideCallCard(), 45000);  // ring TTL
  }

  _hideCallCard() {
    clearTimeout(this._callTimer);
    if (this._callEl) this._callEl.style.display = "none";
  }

  // Paint the captured frame over the VISION + GESTURE viewport (panel 02) for a few seconds.
  // The panel is React-managed, so we overlay a BODY-level fixed <img> aligned to the viewport's
  // box (injecting INTO the panel gets wiped on the next React render — that's why it didn't show).
  _visionFrame(dataUrl) {
    if (!dataUrl) return;
    const lbl = Array.from(document.querySelectorAll("div"))
      .find((d) => d.children.length === 0 && /CAM_01/.test(d.textContent || ""));
    const vp = lbl ? lbl.parentElement : null;
    let img = this._camImg;
    if (!img || !document.body.contains(img)) {
      img = document.createElement("img");
      img.style.cssText = "position:fixed;z-index:9999;object-fit:cover;border-radius:6px;pointer-events:none;border:1px solid rgba(53,231,255,.55);box-shadow:0 0 18px rgba(53,231,255,.4);";
      document.body.appendChild(img);
      this._camImg = img;
    }
    img.src = dataUrl;
    if (vp) {
      const r = vp.getBoundingClientRect();
      img.style.left = r.left + "px"; img.style.top = r.top + "px";
      img.style.width = r.width + "px"; img.style.height = r.height + "px";
    } else {
      // viewport not found — fall back to a small corner preview so it's never invisible
      img.style.right = "18px"; img.style.bottom = "18px"; img.style.left = "auto"; img.style.top = "auto";
      img.style.width = "260px"; img.style.height = "150px";
    }
    img.style.display = "block";
    this.setState({ visionActive: true });
    clearTimeout(this._camTimer);
    this._camTimer = setTimeout(() => {
      if (this._camImg) this._camImg.style.display = "none";
      this.setState({ visionActive: false });
    }, 9000);
  }

  // Like _speak but WITHOUT browser audio — the desktop listener is the one speaking. Status
  // stays 'speaking' until the desktop sends the matching 'idle' (so the HUD tracks real speech);
  // autoIdle=true is for the local greeting, which has no desktop event behind it.
  _speakVisual(text, autoIdle) {
    clearTimeout(this._idleTimer); clearTimeout(this._safetyTimer);
    this.setState({ mode: "speaking", activeModule: null });
    this._streamText(text, "jarvis", autoIdle ? () => {
      this._idleTimer = setTimeout(() => this.setState({ mode: "idle" }), 900);
    } : null);
    if (!autoIdle) this._safetyTimer = setTimeout(() => this.setState({ mode: "idle" }), 25000);
  }

  // ===== live memory graph: pull the real ego-graph (center + neighbours) from the backend =====
  _initMemGraph() {
    const pull = () => {
      fetch(window.JARVIS_API + "/memory/graph")
        .then(r => r.json())
        .then(d => { if (d) this.setState({ memGraph: d }, () => this._renderMemSvg(d)); })
        .catch(() => {});
    };
    pull();
    this._memTimer = setInterval(pull, 30000);
  }

  // Draw the memory-graph nodes/labels by hand (SVG <text> won't take {{ }} props reliably).
  _renderMemSvg(d) {
    const el = this._memSvg; if (!el) return;
    const esc = (s) => String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const nodes = (d && d.nodes) || [];
    const P = [
      { cx: 58, cy: 32, r: 9, tx: 58, ty: 20 }, { cx: 60, cy: 100, r: 9, tx: 60, ty: 116 },
      { cx: 212, cy: 34, r: 9, tx: 212, ty: 22 }, { cx: 216, cy: 98, r: 9, tx: 216, ty: 114 },
      { cx: 135, cy: 18, r: 8, tx: 135, ty: 10 }
    ];
    let s = '<g stroke="rgba(53,231,255,.35)" stroke-width="1" fill="none" stroke-dasharray="3 3" style="animation:edgeflow 1.4s linear infinite;">';
    P.forEach((p, i) => { if (nodes[i]) s += '<line x1="135" y1="65" x2="' + p.cx + '" y2="' + p.cy + '"></line>'; });
    s += '</g><g font-family="Share Tech Mono, monospace" font-size="8" fill="#bdeeff">';
    s += '<circle cx="135" cy="65" r="16" fill="rgba(255,178,77,.18)" stroke="#ffb24d" stroke-width="1.2" style="animation:nodepulse 3s ease-in-out infinite;"></circle>';
    s += '<text x="135" y="68" text-anchor="middle" fill="#ffce8a">' + esc((d && d.center) || "") + '</text>';
    P.forEach((p, i) => {
      if (!nodes[i]) return;
      s += '<circle cx="' + p.cx + '" cy="' + p.cy + '" r="' + p.r + '" fill="rgba(53,231,255,.15)" stroke="#35e7ff" style="animation:nodepulse 3s ease-in-out infinite ' + (0.3 * (i + 1)).toFixed(1) + 's;"></circle>';
      s += '<text x="' + p.tx + '" y="' + p.ty + '" text-anchor="middle">' + esc(nodes[i]) + '</text>';
    });
    s += '</g>';
    el.innerHTML = s;
  }

  // ===== live ticker: pull real crypto / news / weather from the backend every 60s =====
  _initTicker() {
    const pull = () => {
      fetch(window.JARVIS_API + "/ticker")
        .then(r => r.json())
        .then(d => { if (d && d.items && d.items.length) this.setState({ tickerItems: d.items }); })
        .catch(() => {});
    };
    pull();
    this._tickerTimer = setInterval(pull, 60000);
  }

  // ===== live UNIFIED COMMS (panel 06): real WhatsApp / Instagram / email from the backend =====
  // Replaces the canned msgRaw. Refreshes every 20s and immediately on a 'notify' bus event
  // (a new message just landed). Once we've fetched once, the panel shows ONLY real data —
  // even if that's an empty inbox (no fake messages, per the finality rule).
  _initComms() {
    this._pullComms();
    this._commsTimer = setInterval(() => this._pullComms(), 20000);
  }

  _pullComms() {
    fetch(window.JARVIS_API + "/messaging/inbox?limit=6")
      .then(r => r.json())
      .then(d => {
        const meta = {
          whatsapp:  { ch: "WA", chColor: "#4dffb0", chBg: "rgba(77,255,176,.16)" },
          instagram: { ch: "IG", chColor: "#ff8ad1", chBg: "rgba(255,93,177,.16)" },
          email:     { ch: "@",  chColor: "#ffce8a", chBg: "rgba(255,178,77,.16)" }
        };
        const items = ((d && d.items) || []).map((it) => {
          const mm = meta[it.channel] || { ch: "?", chColor: "#7df0ff", chBg: "rgba(53,231,255,.16)" };
          return { ch: mm.ch, from: it.from || "Unknown", txt: it.preview || "",
                   hot: it.importance === "high", chColor: mm.chColor, chBg: mm.chBg };
        });
        let n = 0; const u = (d && d.unread) || {};
        Object.keys(u).forEach((k) => { n += u[k]; });
        this.setState({ commsItems: items, commsUnread: n });
      })
      .catch(() => {});
  }

  _onWakeResult(e) {
    const r = e.results[e.results.length - 1];
    const raw = (r[0] && r[0].transcript) || "";

    // In command mode we're capturing what the user says AFTER the wake word.
    if (this._cmdMode) {
      if (!r.isFinal) { this.setState({ userTranscript: raw.trim() }); return; }
      clearTimeout(this._cmdTimer); this._cmdMode = false;
      const cmd = raw.trim();
      if (cmd) this._ask(cmd); else this.setState({ mode: "idle" });
      return;
    }
    if (!r.isFinal) return;

    // Detect the wake phrase (fuzzy — accent/mishear tolerant), then keep the real command
    // from the ORIGINAL transcript so casing and numbers survive.
    const low = raw.toLowerCase().replace(/[^a-z ]+/g, " ").replace(/\s+/g, " ").trim();
    if (!/(?:wake up|wakeup|wake)\s+(?:jarvis|jervis|jaravis|jarwis|jarviss|travis)\b/.test(low)) return;
    const after = raw.replace(/^.*?(?:jarvis|jervis|jaravis|jarwis|jarviss|travis)[\s,.!?:-]*/i, "").trim();

    if (after) {
      this._ask(after);                       // one-breath: "wake up jarvis, what's the weather"
    } else {
      this.setState({ mode: "listening", caption: "", userTranscript: "" });
      this._cmdMode = true;                   // silently capture the next utterance as the command
      this._cmdTimer = setTimeout(() => {
        this._cmdMode = false;
        if (this.state.mode === "listening") this.setState({ mode: "idle" });
      }, 9000);
    }
  }

  async _startRec() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }
      });
      const AC = window.AudioContext || window.webkitAudioContext;
      const ctx = new AC();
      const src = ctx.createMediaStreamSource(stream);
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      const chunks = [];
      proc.onaudioprocess = (e) => { chunks.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
      src.connect(proc); proc.connect(ctx.destination);
      this._recState = { stream, ctx, proc, src, chunks, sr: ctx.sampleRate, recording: true };
      this.setState({ mode: "listening", caption: "", userTranscript: "" });
    } catch (e) {
      this._speak("I can't reach your microphone, sir. Check the browser's mic permission.");
    }
  }

  _stopRec() {
    const r = this._recState; if (!r || !r.recording) return; r.recording = false;
    try { r.proc.disconnect(); r.src.disconnect(); r.stream.getTracks().forEach(t => t.stop()); } catch (e) {}
    let total = 0; r.chunks.forEach(c => total += c.length);
    const buf = new Float32Array(total); let o = 0; r.chunks.forEach(c => { buf.set(c, o); o += c.length; });
    try { r.ctx.close(); } catch (e) {}
    if (total < r.sr * 0.2) { this.setState({ mode: "idle" }); return; }  // too short
    const wav = this._encodeWav(buf, r.sr, 16000);
    this.setState({ mode: "thinking" });
    const fd = new FormData();
    fd.append("file", new Blob([wav], { type: "audio/wav" }), "mic.wav");
    fetch(window.JARVIS_API + "/voice/stt", { method: "POST", body: fd })
      .then(res => res.json())
      .then(d => { const t = (d && d.text || "").trim(); if (t) this._ask(t); else this.setState({ mode: "idle" }); })
      .catch(() => this.setState({ mode: "idle" }));
  }

  _encodeWav(samples, inRate, outRate) {
    let data = samples;
    if (outRate && outRate !== inRate) {
      const ratio = inRate / outRate, n = Math.floor(samples.length / ratio);
      data = new Float32Array(n);
      for (let i = 0; i < n; i++) data[i] = samples[Math.floor(i * ratio)];
    }
    const rate = outRate || inRate;
    const ab = new ArrayBuffer(44 + data.length * 2), view = new DataView(ab);
    const ws = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
    ws(0, "RIFF"); view.setUint32(4, 36 + data.length * 2, true); ws(8, "WAVE"); ws(12, "fmt ");
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    ws(36, "data"); view.setUint32(40, data.length * 2, true);
    let off = 44; for (let i = 0; i < data.length; i++) { let s = Math.max(-1, Math.min(1, data[i])); view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7FFF, true); off += 2; }
    return ab;
  }
"""


def decode_bundle(html: str):
    man = json.loads(re.search(r'<script type="__bundler/manifest">(.*?)</script>', html, re.DOTALL).group(1))
    tpl = json.loads(re.search(r'<script type="__bundler/template">(.*?)</script>', html, re.DOTALL).group(1))
    assets = {}
    for uuid, e in man.items():
        raw = base64.b64decode(e["data"])
        if e.get("compressed"):
            raw = gzip.decompress(raw)
        assets[uuid] = {"bytes": raw, "mime": e.get("mime", "")}
    return tpl, assets


def patch_component(js: str) -> str:
    # 1. wake label -> JARVIS phrase, and make it reflect the live state (not just listening)
    js = js.replace("\"AWAITING 'WAKEUP FRIDAY'\"", "\"AWAITING 'WAKE UP JARVIS'\"")
    js = re.sub(
        r'wakeLabel: listening \? "[^"]*" : "[^"]*",',
        'wakeLabel: mode === "listening" ? "HEARING YOU…" : '
        '(mode === "thinking" ? "PROCESSING…" : '
        '(mode === "speaking" ? "RESPONDING…" : "AWAITING \'WAKE UP JARVIS\'")),',
        js, count=1)
    # 2. intro greeting -> JARVIS persona; subscribe to the live voice bus + start the ticker.
    #    (Hands-free voice is handled by the proven desktop listener, mirrored here via /events.)
    # Greeting is VISUAL-ONLY (_speakVisual, no browser audio) — the desktop listener is the
    # voice. Otherwise the PWA would talk over the PC on every reload.
    js = re.sub(r'this\._speak\("Welcome back, Boss[^"]*"\);',
                'this._speakVisual("Welcome back, sir. All systems online. Standing by.", true); '
                'this._initEvents(); this._initTicker(); this._initMemGraph(); this._initComms(); '
                'this._initIdentity();', js)
    # 2b. make the top ticker + memory graph + comms realtime: seed state slots, read them
    js = js.replace('    time: "--:--:--", date: "",',
                    '    time: "--:--:--", date: "",\n    tickerItems: null,\n    memGraph: null,'
                    '\n    commsItems: null,\n    commsUnread: 0,\n    emReal: null,'
                    '\n    identityLine: "ADITYA · OWNER", identityVp: "VOICEPRINT 99.2%",'
                    '\n    identityInitial: "A", identityStatus: "VERIFIED",', 1)
    # 2d. Phase 5: the EMOTION panel reads JARVIS's LIVE register + 4 axes (pushed on the 'mood'
    #     bus event) when present, falling back to the mode-based mockup before the first turn.
    js = js.replace("const em = EMO[mode];", "const em = this.state.emReal || EMO[mode];", 1)
    js = js.replace('const tickerItems = [', 'const tickerItems = this.state.tickerItems || [', 1)
    # 2c. UNIFIED COMMS (panel 06): drive the message list + "NEW" badge from real backend data
    #     (fall back to the mockup msgRaw only until the first /messaging/inbox fetch lands).
    js = js.replace('const messages = msgRaw.map(',
                    'const messages = (this.state.commsItems || msgRaw).map(', 1)
    # feed the MEMORY GRAPH placeholders from state.memGraph (fallback to a dot)
    js = js.replace(
        "    return {\n      rootRef: (el) => { this._root = el; },",
        "    return {\n"
        "      memStat: (this.state.memGraph && this.state.memGraph.stat) || '3-TIER',\n"
        "      memSvgRef: (el) => { this._memSvg = el; },\n"
        "      inboxCount: (this.state.commsItems ? (this.state.commsUnread || 0) : 9) + ' NEW',\n"
        "      identityLine: this.state.identityLine, identityVp: this.state.identityVp,\n"
        "      identityInitial: this.state.identityInitial, identityStatus: this.state.identityStatus,\n"
        "      rootRef: (el) => { this._root = el; },", 1)
    # 3. append backend-wiring overrides just before the class's final closing brace
    js = js.rstrip()
    assert js.endswith("}"), "unexpected dc-script tail"
    return js[:-1] + OVERRIDES + "\n}\n"


# =========================================================================================== #
# MOBILE BUILD — Aditya's `JARVIS-Mobile.html` phone design, wired to the SAME backend.        #
# Emitted as a SEPARATE app/web/mobile.html (+ its own dc-runtime-mobile.js) so the PC shell    #
# (index.html / sw.js / manifest) is never touched — the desktop HUD stays byte-for-byte the    #
# same. web.py serves this file only to phones (User-Agent). The phone's own status bar already #
# shows the clock + battery, so the in-app status bar is stripped out.                          #
# =========================================================================================== #

# Backend-wiring overrides appended to the mobile component class (last def wins). The mobile
# mockup's method names differ from the desktop HUD's, so this is its own set — but it reuses the
# very same endpoints (/chat, /voice/*, /events/ws, /messaging/inbox, /memory/graph, /calls/*).
OVERRIDES_MOBILE = r"""

  // ===== real JARVIS backend wiring (replaces the mockup scenarios) =====
  _ask = (txt) => {
    txt = (txt || "").trim(); if (!txt) return;
    this._clearStream(); clearTimeout(this._idleTimer);
    // a "what is this / use the camera" question uses THIS phone's camera, not the PC's.
    if (/\b(what(?:'?s| is)?\s+this|what am i (?:holding|showing|looking at)|look at this|can you see this|use (?:the |my )?camera)\b/i.test(txt) && !/screen/i.test(txt)) {
      this._lookCam(txt); return;
    }
    this.setState({ mode: "thinking", activeModule: null, caption: "" });
    fetch(window.JARVIS_API + "/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: txt, session_id: window.JARVIS_SID })
    })
      .then(r => r.json())
      .then(d => { this._speak((d && d.reply) || "I couldn't reach my services just now, sir.", d && d.module); this._checkEnrollment(); })
      .catch(() => this._speak("I couldn't reach my services just now, sir."));
  };

  // ===== Phase 11.2 — guided enrolment ON the phone (after the Owner says "add Vikram as trusted").
  // Mirrors the PC listener: capture a face selfie + three spoken lines, then enrol — auto-driven,
  // reusing the mic + front camera. Authorised by the Owner's verified session (no token shipped).
  _checkEnrollment() {
    if (this._enrolling) return;
    fetch(window.JARVIS_API + "/identity/enroll/pending?session=" + encodeURIComponent(window.JARVIS_SID || ""))
      .then(r => r.json()).then(p => { if (p && p.name) this._runEnrollment(p); }).catch(() => {});
  }

  async _runEnrollment(p) {
    if (this._enrolling) return;
    this._enrolling = true;
    const sid = encodeURIComponent(window.JARVIS_SID || "");
    const sents = p.sentences || ["Please read this line aloud."];
    try {
      await this._sayEnrol(p.name + ", look at the camera for me.");
      try {
        const face = await this._captureFace();
        if (face) { const ff = new FormData(); ff.append("file", face, "face.jpg");
          await fetch(window.JARVIS_API + "/identity/enroll/face?session=" + sid, { method: "POST", body: ff }); }
      } catch (e) {}
      const need = p.need_voice || 3;
      for (let i = 0; i < need; i++) {
        await this._sayEnrol("Now read: " + sents[i % sents.length]);
        const clip = await this._recordClip(4500);
        if (clip) { const vf = new FormData(); vf.append("file", clip, "v.wav");
          await fetch(window.JARVIS_API + "/identity/enroll/voice?session=" + sid, { method: "POST", body: vf }).catch(() => {}); }
      }
      let msg = "Enrolment complete, sir.";
      try { const r = await fetch(window.JARVIS_API + "/identity/enroll/finalize?session=" + sid, { method: "POST" });
            const d = await r.json(); if (d && d.message) msg = d.message; } catch (e) {}
      this._speak(msg);
    } finally { this._enrolling = false; }
  }

  // speak a prompt and WAIT until it finishes, so we don't record over JARVIS's own voice
  _sayEnrol(line) {
    return new Promise((resolve) => {
      this.setState({ mode: "speaking", caption: line });
      this._streamText(line, null);
      this._playTTS(line);
      setTimeout(resolve, Math.min(6000, 1200 + line.length * 55));
    });
  }

  _recordClip(ms) {
    return navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } })
      .then((stream) => new Promise((resolve) => {
        const AC = window.AudioContext || window.webkitAudioContext; const ctx = new AC();
        const src = ctx.createMediaStreamSource(stream); const proc = ctx.createScriptProcessor(4096, 1, 1);
        const chunks = []; proc.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
        src.connect(proc); proc.connect(ctx.destination);
        this.setState({ mode: "listening", caption: "● listening…" });
        setTimeout(() => {
          try { proc.disconnect(); src.disconnect(); stream.getTracks().forEach(t => t.stop()); } catch (e) {}
          let total = 0; chunks.forEach(c => total += c.length);
          const buf = new Float32Array(total); let o = 0; chunks.forEach(c => { buf.set(c, o); o += c.length; });
          try { ctx.close(); } catch (e) {}
          resolve(total < ctx.sampleRate * 0.3 ? null : new Blob([this._encodeWav(buf, ctx.sampleRate || 16000, 16000)], { type: "audio/wav" }));
        }, ms);
      })).catch(() => null);
  }

  async _captureFace() {
    let stream = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 640 } } });
      const v = document.createElement("video"); v.srcObject = stream; v.muted = true; v.playsInline = true; v.setAttribute("playsinline", "");
      await v.play().catch(() => {}); await new Promise((r) => setTimeout(r, 500));
      const w = v.videoWidth || 640, h = v.videoHeight || 480;
      const c = document.createElement("canvas"); c.width = w; c.height = h; c.getContext("2d").drawImage(v, 0, 0, w, h);
      return await new Promise((res) => c.toBlob(res, "image/jpeg", 0.85));
    } catch (e) { return null; }
    finally { try { stream.getTracks().forEach((t) => t.stop()); } catch (e) {} }
  }

  _send = () => {
    const el = this._input; if (!el) return;
    const txt = el.value.trim(); if (!txt) return; el.value = "";
    this._ask(txt);
  };

  _runScenario = (key) => {
    const m = {
      research: "Give me a short brief on the latest in AI today.",
      inbox: "What unread messages do I have?",
      vision: "What am I looking at?",
      feeds: "Give me a quick world news brief.",
      identity: "Who has access to you right now?",
      memory: "What am I working on right now?"
    };
    this._ask(m[key] || key);
  };

  // _speak (direct phone interaction) DOES voice the reply via TTS. The mockup had no audio.
  _speak(text, module) {
    this.setState({ mode: "speaking", activeModule: module || null });
    this._playTTS(text);
    this._streamText(text, () => { this._idleTimer = setTimeout(() => this.setState({ mode: "idle" }), 900); });
  }

  // _speakVisual streams the caption WITHOUT audio — used for the PC voice mirror (the desktop is
  // already speaking aloud) and the local greeting, so the phone never talks over the PC.
  _speakVisual(text, autoIdle) {
    clearTimeout(this._idleTimer); clearTimeout(this._safetyTimer);
    this.setState({ mode: "speaking", activeModule: null });
    this._streamText(text, autoIdle ? () => { this._idleTimer = setTimeout(() => this.setState({ mode: "idle" }), 900); } : null);
    if (!autoIdle) this._safetyTimer = setTimeout(() => this.setState({ mode: "idle" }), 25000);
  }

  async _playTTS(text) {
    try {
      if (this._audio) { try { this._audio.pause(); } catch (e) {} }
      const r = await fetch(window.JARVIS_API + "/voice/tts/stream", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text })
      });
      if (!r.ok) return;
      const url = URL.createObjectURL(await r.blob());
      const a = new Audio(url); this._audio = a;
      a.onended = () => URL.revokeObjectURL(url);
      a.play().catch(() => {});
    } catch (e) {}
  }

  // ===== mic -> /voice/stt (free Whisper) =====
  _onMic = () => {
    if (this.state.mode === "thinking") return;
    if (this._recState && this._recState.recording) { this._stopRec(); return; }
    this._startRec();
  };

  async _startRec() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
      const AC = window.AudioContext || window.webkitAudioContext;
      const ctx = new AC();
      const src = ctx.createMediaStreamSource(stream);
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      const chunks = [];
      proc.onaudioprocess = (e) => { chunks.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
      src.connect(proc); proc.connect(ctx.destination);
      this._recState = { stream, ctx, proc, src, chunks, sr: ctx.sampleRate, recording: true };
      this.setState({ mode: "listening", caption: "" });
    } catch (e) {
      this._speak("I can't reach your microphone, sir. Check the browser's mic permission.");
    }
  }

  _stopRec() {
    const r = this._recState; if (!r || !r.recording) return; r.recording = false;
    try { r.proc.disconnect(); r.src.disconnect(); r.stream.getTracks().forEach(t => t.stop()); } catch (e) {}
    let total = 0; r.chunks.forEach(c => total += c.length);
    const buf = new Float32Array(total); let o = 0; r.chunks.forEach(c => { buf.set(c, o); o += c.length; });
    try { r.ctx.close(); } catch (e) {}
    if (total < r.sr * 0.2) { this.setState({ mode: "idle" }); return; }
    const wav = this._encodeWav(buf, r.sr, 16000);
    this.setState({ mode: "thinking" });
    const blob = new Blob([wav], { type: "audio/wav" });
    const fd = new FormData(); fd.append("file", blob, "mic.wav");
    // Phase 11.2 - identify the speaker (server-verified) IN PARALLEL with STT, so the phone gates
    // by who's actually talking. The voiceprint (~50ms) hides under the STT round-trip.
    const idfd = new FormData();
    idfd.append("file", blob, "mic.wav");
    idfd.append("session", window.JARVIS_SID || "pwa");
    Promise.all([
      fetch(window.JARVIS_API + "/identity/whoami", { method: "POST", body: idfd }).then(r2 => r2.json()).catch(() => null),
      fetch(window.JARVIS_API + "/voice/stt", { method: "POST", body: fd }).then(r2 => r2.json()).catch(() => null),
    ]).then(([who, sttd]) => {
      if (who && who.tier === "stranger") { this._strangerDeflect(); return; }
      const t = (sttd && sttd.text || "").trim();
      if (t) this._ask(t); else this.setState({ mode: "idle" });
    }).catch(() => this.setState({ mode: "idle" }));
  }

  // An unrecognised voice on the phone gets the same in-character deflection as on the PC.
  _strangerDeflect() {
    this.setState({ caption: "" });
    this._speak("You're a stranger to me - not on " + (window.JARVIS_OWNER || "the owner") +
                "'s list of trusted people - so I can't answer you.");
  }

  _encodeWav(samples, inRate, outRate) {
    let data = samples;
    if (outRate && outRate !== inRate) {
      const ratio = inRate / outRate, n = Math.floor(samples.length / ratio);
      data = new Float32Array(n);
      for (let i = 0; i < n; i++) data[i] = samples[Math.floor(i * ratio)];
    }
    const rate = outRate || inRate;
    const ab = new ArrayBuffer(44 + data.length * 2), view = new DataView(ab);
    const ws = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
    ws(0, "RIFF"); view.setUint32(4, 36 + data.length * 2, true); ws(8, "WAVE"); ws(12, "fmt ");
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    ws(36, "data"); view.setUint32(40, data.length * 2, true);
    let off = 44; for (let i = 0; i < data.length; i++) { let s = Math.max(-1, Math.min(1, data[i])); view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7FFF, true); off += 2; }
    return ab;
  }

  // ===== this phone's camera -> /vision/describe =====
  async _lookCam(question) {
    this.setState({ mode: "thinking", activeModule: "vision", caption: "" });
    let stream = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 1280 } } });
    } catch (e) {
      this._speak("I can't reach your camera, sir — check the browser's camera permission."); return;
    }
    try {
      const video = document.createElement("video");
      video.srcObject = stream; video.muted = true; video.playsInline = true; video.setAttribute("playsinline", "");
      await video.play().catch(() => {});
      await new Promise((r) => setTimeout(r, 500));
      const w = video.videoWidth || 1280, h = video.videoHeight || 720;
      const canvas = document.createElement("canvas"); canvas.width = w; canvas.height = h;
      canvas.getContext("2d").drawImage(video, 0, 0, w, h);
      const dataUrl = canvas.toDataURL("image/jpeg", 0.72);
      const r = await fetch(window.JARVIS_API + "/vision/describe", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ image: dataUrl, question })
      });
      const d = await r.json();
      this._speak((d && d.ok && d.text) || (d && d.error) || "I couldn't make that out, sir.", "vision");
    } catch (e) {
      this._speak("Something went wrong with the camera, sir.");
    } finally {
      try { stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
    }
  }

  // ===== live voice mirror: the PC desktop listener drives this phone HUD over a WebSocket =====
  _initEvents() {
    if (this._evtInited) return; this._evtInited = true;
    const connect = () => {
      let ws;
      try {
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(proto + "//" + location.host + "/events/ws");
      } catch (e) { setTimeout(connect, 2500); return; }
      this._evtWs = ws;
      ws.onmessage = (m) => { try { this._onVoiceEvent(JSON.parse(m.data)); } catch (e) {} };
      ws.onclose = () => { setTimeout(connect, 2500); };
      ws.onerror = () => { try { ws.close(); } catch (e) {} };
    };
    connect();
  }

  _onVoiceEvent(ev) {
    if (!ev || !ev.type) return;
    if (ev.type === "listening") {
      this._clearStream(); clearTimeout(this._idleTimer); clearTimeout(this._safetyTimer);
      this.setState({ mode: "listening", caption: "" });
    } else if (ev.type === "transcript") {
      this._clearStream(); clearTimeout(this._idleTimer); clearTimeout(this._safetyTimer);
      this.setState({ mode: "thinking", caption: "" });
    } else if (ev.type === "reply") {
      if (ev.text) this._speakVisual(ev.text); else this.setState({ mode: "idle" });
    } else if (ev.type === "idle") {
      clearTimeout(this._safetyTimer);
      this._idleTimer = setTimeout(() => this.setState({ mode: "idle" }), 250);
    } else if (ev.type === "notify") {
      if (this._pullComms) this._pullComms();
    } else if (ev.type === "call") {
      if (ev.kind === "incoming") this._showCall(ev);
      else { clearTimeout(this._callTimer); this.setState({ incomingCall: false }); }
    } else if (ev.type === "identity") {
      this._setIdentity(ev);          // Phase 11 — who's using JARVIS -> header
    }
  }

  // ===== Phase 11 identity — show who is using JARVIS in the header =====
  _initIdentity() {
    fetch(window.JARVIS_API + "/identity/active")
      .then(r => r.json()).then(d => { if (d) this._setIdentity(d); }).catch(() => {});
  }

  _setIdentity(d) {
    if (!d) return;
    const tier = d.tier || "owner";
    const nm = (d.name || "") + (tier && tier !== "owner" ? " · " + tier.toUpperCase() : "");
    this.setState({ idInitial: d.initial || "A", idName: nm || "OWNER" });
  }

  // ===== Phase 8 calls: a real incoming ring drives the full-screen call overlay =====
  _showCall(ev) {
    const name = ev.name || ev.number || "Unknown";
    const initial = (ev.name || ev.number || "?").trim().charAt(0).toUpperCase();
    this.setState({ incomingCall: true, callInfo: { name, number: ev.number || "", initial } });
    clearTimeout(this._callTimer);
    this._callTimer = setTimeout(() => this.setState({ incomingCall: false }), 45000);  // ring TTL
  }

  _callAction(action) {
    clearTimeout(this._callTimer);
    this.setState({ incomingCall: false });
    fetch(window.JARVIS_API + "/calls/command", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action })
    }).catch(() => {});
  }

  // ===== UNIFIED COMMS: real WhatsApp / Instagram / email from the backend =====
  _initComms() {
    this._pullComms();
    this._commsTimer = setInterval(() => this._pullComms(), 20000);
  }

  _pullComms() {
    fetch(window.JARVIS_API + "/messaging/inbox?limit=6")
      .then(r => r.json())
      .then(d => {
        const meta = {
          whatsapp:  { ch: "WA", chColor: "#4dffb0", chBg: "rgba(77,255,176,.16)" },
          instagram: { ch: "IG", chColor: "#ff8ad1", chBg: "rgba(255,93,177,.16)" },
          email:     { ch: "@",  chColor: "#ffce8a", chBg: "rgba(255,178,77,.16)" }
        };
        const items = ((d && d.items) || []).map((it) => {
          const mm = meta[it.channel] || { ch: "?", chColor: "#7df0ff", chBg: "rgba(53,231,255,.16)" };
          return { ch: mm.ch, from: it.from || "Unknown", txt: it.preview || "",
                   hot: it.importance === "high", chColor: mm.chColor, chBg: mm.chBg };
        });
        let n = 0; const u = (d && d.unread) || {};
        Object.keys(u).forEach((k) => { n += u[k]; });
        this.setState({ commsItems: items, commsUnread: n });
      })
      .catch(() => {});
  }

  // ===== MEMORY GRAPH: real ego-graph from /memory/graph (same node layout as the design) =====
  _initMemGraph() {
    const pull = () => fetch(window.JARVIS_API + "/memory/graph")
      .then(r => r.json())
      .then(d => { if (d) this.setState({ memGraph: d }, () => this._renderMemSvgM(d)); })
      .catch(() => {});
    pull();
    this._memTimer = setInterval(pull, 30000);
  }

  _renderMemSvgM(d) {
    const el = this._memSvg; if (!el) return;
    const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const nodes = (d && d.nodes) || [];
    const P = [
      { cx: 64, cy: 34, r: 10, tx: 64, ty: 20 }, { cx: 68, cy: 108, r: 10, tx: 68, ty: 126 },
      { cx: 256, cy: 36, r: 10, tx: 256, ty: 22 }, { cx: 252, cy: 106, r: 10, tx: 252, ty: 124 },
      { cx: 160, cy: 20, r: 9, tx: 160, ty: 11 }
    ];
    let s = '<g stroke="rgba(53,231,255,.35)" stroke-width="1" fill="none" stroke-dasharray="3 3" style="animation:edgeflow 1.4s linear infinite;">';
    P.forEach((p, i) => { if (nodes[i]) s += '<line x1="160" y1="70" x2="' + p.cx + '" y2="' + p.cy + '"></line>'; });
    s += '</g><g font-family="Share Tech Mono, monospace" font-size="9" fill="#bdeeff">';
    s += '<circle cx="160" cy="70" r="18" fill="rgba(255,178,77,.18)" stroke="#ffb24d" stroke-width="1.2" style="animation:nodepulse 3s ease-in-out infinite;"></circle>';
    s += '<text x="160" y="73" text-anchor="middle" fill="#ffce8a">' + esc((d && d.center) || "") + '</text>';
    P.forEach((p, i) => {
      if (!nodes[i]) return;
      s += '<circle cx="' + p.cx + '" cy="' + p.cy + '" r="' + p.r + '" fill="rgba(53,231,255,.15)" stroke="#35e7ff" style="animation:nodepulse 3s ease-in-out infinite ' + (0.3 * (i + 1)).toFixed(1) + 's;"></circle>';
      s += '<text x="' + p.tx + '" y="' + p.ty + '" text-anchor="middle">' + esc(nodes[i]) + '</text>';
    });
    s += '</g>';
    el.innerHTML = s;
  }
"""


# The mobile shell. Token-replaced (not an f-string) so the inline JS braces need no escaping.
MOBILE_INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#02040a">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="JARVIS">
<title>J.A.R.V.I.S</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/static/icons/icon-192.png">
<link rel="icon" href="/static/icons/icon-192.png">
<style>html,body{margin:0;background:#000;overflow:hidden;}</style>
<script>
  window.JARVIS_API = location.origin;
  window.JARVIS_OWNER = "@@OWNER@@";
  window.JARVIS_SID = localStorage.getItem('jarvis_sid')
    || (function(){var s=Date.now().toString(36)+Math.random().toString(36).slice(2);localStorage.setItem('jarvis_sid',s);return s;})();
</script>
<script src="/static/vendor/react.production.min.js"></script>
<script src="/static/vendor/react-dom.production.min.js"></script>
<script src="/static/vendor/babel.min.js"></script>
<script src="/static/dc-runtime-mobile.js"></script>
</head>
<body>
<x-dc>@@MARKUP@@</x-dc>
<script @@DCATTRS@@>@@PATCHED@@</script>
<script>
  if ('serviceWorker' in navigator) {
    let _reloaded = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (_reloaded) return; _reloaded = true; location.reload();
    });
    window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js')
      .then(reg => reg.update()).catch(()=>{}));
  }
</script>
</body>
</html>
"""


def patch_mobile_markup(markup: str) -> str:
    # 1. Strip the in-app status bar (clock + 5G + signal + battery) — the phone's own OS status
    #    bar already shows all of that, so it was duplicated. Everything else is preserved.
    markup = re.sub(r'<!-- ===== STATUS BAR ===== -->.*?<!-- ===== HEADER ===== -->',
                    '<!-- ===== HEADER ===== -->', markup, count=1, flags=re.DOTALL)
    # 2. UNIFIED COMMS badge -> real unread count.
    markup = markup.replace(">9 NEW<", ">{{ inboxCount }}<", 1)
    # 3. MEMORY GRAPH: bind the stat, and swap the hand-drawn nodes for a ref'd <svg> we paint
    #    imperatively from real /memory/graph data (keeping the exact node positions).
    markup = re.sub(r">3-TIER[^<]*<", ">{{ memStat }}<", markup, count=1)
    markup = re.sub(r'<svg viewBox="0 0 320 140".*?</svg>',
                    '<svg viewBox="0 0 320 140" style="width:100%; height:140px;" ref="{{ memSvgRef }}"></svg>',
                    markup, count=1, flags=re.DOTALL)
    # 4. INCOMING CALL overlay: bind the avatar initial, name and number to the real caller.
    markup = markup.replace('font-size:38px; color:#eafdff;">M</div>',
                            'font-size:38px; color:#eafdff;">{{ callInitial }}</div>', 1)
    markup = markup.replace('>MOM</div>', '>{{ callName }}</div>', 1)
    markup = re.sub(r'>\+91[^<]*</div>', '>{{ callNumber }}</div>', markup, count=1)
    # 5. Phase 11 — header shows WHO is using JARVIS: the avatar initial + a small name line.
    markup = markup.replace('font-size:14px; color:#eafdff;">A</span>',
                            'font-size:14px; color:#eafdff;">{{ idInitial }}</span>', 1)
    markup = markup.replace(
        'line-height:1;">J.A.R.V.I.S</div>',
        'line-height:1;">J.A.R.V.I.S</div>'
        '<div style="font-family:\'Share Tech Mono\',monospace; font-size:8px; letter-spacing:1px; '
        'color:#7fd0e8; margin-top:3px;">{{ idName }}</div>', 1)
    return markup


def patch_mobile_component(js: str) -> str:
    # 1. wake label -> JARVIS persona (never "Friday").
    js = js.replace('"SAY \'WAKEUP FRIDAY\'"', '"SAY \'WAKE UP JARVIS\'"', 1)
    # 2. greeting (visual-only, so the phone never talks over the PC) + start the live wiring.
    js = re.sub(r'\(\)\s*=>\s*this\._speak\("Welcome back, Boss[^"]*"\)',
                '() => { this._speakVisual("Welcome back, sir. All systems online. Standing by.", true); '
                'this._initEvents(); this._initComms(); this._initMemGraph(); this._initIdentity(); }', js, count=1)
    # 3. extra state slots for the live panels + caller + active identity.
    js = js.replace('incomingCall: false,\n    activeModule: null,',
                    'incomingCall: false,\n    activeModule: null,\n'
                    '    commsItems: null, commsUnread: 0, memGraph: null, callInfo: null,\n'
                    '    idInitial: "A", idName: "OWNER",', 1)
    # 4. UNIFIED COMMS list from real backend data (fall back to the mockup until the first fetch).
    js = js.replace('const messages = msgRaw.map(',
                    'const messages = (this.state.commsItems || msgRaw).map(', 1)
    # 5. renderVals: memory-graph stat + ref.
    js = js.replace(
        '      stageRef: (el) => { this._stage = el; },',
        '      stageRef: (el) => { this._stage = el; },\n'
        "      memStat: (this.state.memGraph && this.state.memGraph.stat) || '3-TIER · 12.4k',\n"
        '      idInitial: this.state.idInitial, idName: this.state.idName,\n'
        '      memSvgRef: (el) => { this._memSvg = el; if (el && this.state.memGraph) this._renderMemSvgM(this.state.memGraph); },', 1)
    # 6. renderVals: comms badge count.
    js = js.replace('messages, inboxActive: am === "inbox",',
                    "messages, inboxCount: (this.state.commsItems ? (this.state.commsUnread || 0) : 9) + ' NEW', "
                    'inboxActive: am === "inbox",', 1)
    # 7. renderVals: bind the live caller, and route the overlay buttons to the real command queue.
    js = js.replace(
        '      incomingCall: this.state.incomingCall,',
        '      incomingCall: this.state.incomingCall,\n'
        '      callInitial: (this.state.callInfo && this.state.callInfo.initial) || ((this.state.callInfo && this.state.callInfo.name) ? this.state.callInfo.name.trim().charAt(0).toUpperCase() : "?"),\n'
        '      callName: (this.state.callInfo && this.state.callInfo.name) || "Unknown",\n'
        '      callNumber: (this.state.callInfo && this.state.callInfo.number) || "",', 1)
    js = js.replace(
        'onSimCall: () => this.setState({ incomingCall: true }),',
        'onSimCall: () => this.setState({ incomingCall: true, callInfo: { name: "Test Call", number: "", initial: "T" } }),', 1)
    js = js.replace(
        'onAccept: () => { this.setState({ incomingCall: false }); this._clearStream(); this._speak("Connecting you to Mom now, Boss."); },',
        'onAccept: () => this._callAction("answer"),', 1)
    js = js.replace(
        'onDecline: () => { this.setState({ incomingCall: false }); this._clearStream(); this._speak("Declined. I\'ll let her know you\'ll call back this evening.", "inbox"); }',
        'onDecline: () => this._callAction("decline")', 1)
    # 8. append the override methods just before the class's final closing brace.
    js = js.rstrip()
    assert js.endswith("}"), "unexpected mobile dc-script tail"
    return js[:-1] + OVERRIDES_MOBILE + "\n}\n"


def build_mobile() -> None:
    """Build app/web/mobile.html from JARVIS-Mobile.html. Writes ONLY mobile.html + its own
    dc-runtime-mobile.js + fonts — never the PC shell, which is asserted untouched."""
    if not MSRC.exists():
        print(f"[build_pwa] (mobile) skipped — no {MSRC.name}")
        return
    pc_shell = (WEB / "index.html").read_bytes() if (WEB / "index.html").exists() else None

    html = MSRC.read_text(encoding="utf-8", errors="replace")
    tpl, assets = decode_bundle(html)
    # this bundle is a {pages, entry} map (the desktop one is a bare template string).
    page = tpl["pages"][tpl["entry"]] if isinstance(tpl, dict) else tpl

    # dc-runtime to its OWN file so the PC runtime is never overwritten.
    runtime_uuid = next(u for u, a in assets.items()
                        if a["mime"] == "text/javascript" and a["bytes"][:10].startswith(b"// GENER"))
    (STATIC / "dc-runtime-mobile.js").write_bytes(assets[runtime_uuid]["bytes"])

    # fonts (uuid-named; shared dir, no collision with the PC fonts).
    font_uuids = []
    for u, a in assets.items():
        if "font" in a["mime"]:
            (FONTS / f"{u}.woff2").write_bytes(a["bytes"])
            font_uuids.append(u)

    markup = re.search(r'<x-dc[^>]*>(.*)</x-dc>', page, re.DOTALL).group(1)
    dc_tag = re.search(r'<script\s+([^>]*data-dc-script[^>]*)>(.*?)</script>', page, re.DOTALL)
    dc_attrs, dc_js = dc_tag.group(1), dc_tag.group(2)

    for u in font_uuids:
        markup = markup.replace(f'url("{u}")', f'url("/static/fonts/{u}.woff2")')

    markup = patch_mobile_markup(markup)
    patched = patch_mobile_component(dc_js)

    import os
    owner = (os.getenv("JARVIS_USER_NAME", "Aditya").strip() or "Aditya")
    index = (MOBILE_INDEX_TEMPLATE
             .replace("@@MARKUP@@", markup)
             .replace("@@DCATTRS@@", dc_attrs)
             .replace("@@PATCHED@@", patched)
             .replace("@@OWNER@@", owner))
    (WEB / "mobile.html").write_text(index, encoding="utf-8")

    # HARD GUARANTEE: the mobile build must never alter the desktop shell.
    if pc_shell is not None:
        assert (WEB / "index.html").read_bytes() == pc_shell, "mobile build mutated index.html!"

    print(f"[build_pwa] OK -> {WEB / 'mobile.html'}  "
          f"(mobile fonts {len(font_uuids)} | dc-script {len(patched)//1024} KB | PC shell untouched)")


def main() -> None:
    if not SRC.exists():
        sys.exit(f"missing {SRC}")
    for d in (WEB, STATIC, FONTS, VENDOR, ICONS):
        d.mkdir(parents=True, exist_ok=True)

    html = SRC.read_text(encoding="utf-8", errors="replace")
    tpl, assets = decode_bundle(html)

    # dc-runtime js (the one text/javascript asset whose head is "// GENERATED")
    runtime_uuid = next(u for u, a in assets.items()
                        if a["mime"] == "text/javascript" and a["bytes"][:10].startswith(b"// GENER"))
    (STATIC / "dc-runtime.js").write_bytes(assets[runtime_uuid]["bytes"])

    # fonts: write each woff2 and remember which uuids are fonts (for URL rewrite)
    font_uuids = []
    for u, a in assets.items():
        if "font" in a["mime"]:
            (FONTS / f"{u}.woff2").write_bytes(a["bytes"])
            font_uuids.append(u)

    # recover markup + dc-script from the template
    markup = re.search(r'<x-dc[^>]*>(.*)</x-dc>', tpl, re.DOTALL).group(1)
    dc_tag = re.search(r'<script\s+([^>]*data-dc-script[^>]*)>(.*?)</script>', tpl, re.DOTALL)
    dc_attrs, dc_js = dc_tag.group(1), dc_tag.group(2)

    # rewrite font url("uuid") -> url("/static/fonts/uuid.woff2") in the markup's <helmet> CSS
    for u in font_uuids:
        markup = markup.replace(f'url("{u}")', f'url("/static/fonts/{u}.woff2")')

    # MEMORY GRAPH (panel 05): the stat is a normal HTML span (interpolates fine). SVG <text>
    # does NOT interpolate reliably, so blank the node SVG and give it a ref — we draw the real
    # nodes/labels imperatively from JS (_renderMemSvg) with live /memory/graph data.
    markup = re.sub(r">3-TIER[^<]*<", ">{{ memStat }}<", markup, count=1)

    # UNIFIED COMMS (panel 06): make the "9 NEW" badge reflect the real unread count.
    markup = markup.replace(">9 NEW<", ">{{ inboxCount }}<", 1)
    markup = re.sub(r'<svg viewBox="0 0 270 130"[^>]*>.*?</svg>',
                    '<svg viewBox="0 0 270 130" style="width:100%; height:130px;" ref="{{ memSvgRef }}"></svg>',
                    markup, count=1, flags=re.DOTALL)

    # IDENTITY (panel 01): bind the name/role, the biometric line, the avatar initial and the
    # status badge to the LIVE active user (Phase 11). Defaults match the original static text,
    # so the panel looks byte-identical until a real identification arrives.
    markup = markup.replace(">ADITYA · OWNER<", ">{{ identityLine }}<", 1)
    markup = markup.replace(">VOICEPRINT 99.2%<", ">{{ identityVp }}<", 1)
    markup = markup.replace('color:#eafdff;">A</span>', 'color:#eafdff;">{{ identityInitial }}</span>', 1)
    markup = markup.replace("● VERIFIED", "● {{ identityStatus }}", 1)

    patched_js = patch_component(dc_js)

    # vendor libs (download once; cached locally => offline + free, no CDN at runtime)
    for name, url in VENDOR_LIBS.items():
        dest = VENDOR / name
        if dest.exists() and dest.stat().st_size > 1000:
            continue
        print(f"  downloading {name} ...")
        r = httpx.get(url, timeout=60, follow_redirects=True)
        r.raise_for_status()
        dest.write_bytes(r.content)

    index = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#02040a">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="JARVIS">
<title>J.A.R.V.I.S</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/static/icons/icon-192.png">
<link rel="icon" href="/static/icons/icon-192.png">
<style>html,body{{margin:0;background:#02040a;overflow:hidden;}}</style>
<script>
  window.JARVIS_API = location.origin;
  window.JARVIS_SID = localStorage.getItem('jarvis_sid')
    || (function(){{var s=Date.now().toString(36)+Math.random().toString(36).slice(2);localStorage.setItem('jarvis_sid',s);return s;}})();
</script>
<script src="/static/vendor/react.production.min.js"></script>
<script src="/static/vendor/react-dom.production.min.js"></script>
<script src="/static/vendor/babel.min.js"></script>
<script src="/static/dc-runtime.js"></script>
</head>
<body>
<x-dc>{markup}</x-dc>
<script {dc_attrs}>{patched_js}</script>
<script>
  if ('serviceWorker' in navigator) {{
    let _reloaded = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {{
      if (_reloaded) return; _reloaded = true; location.reload();   // new build took over -> refresh once
    }});
    window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js')
      .then(reg => reg.update()).catch(()=>{{}}));
  }}
</script>
</body>
</html>
"""
    (WEB / "index.html").write_text(index, encoding="utf-8")

    import hashlib
    version = hashlib.sha1(index.encode("utf-8")).hexdigest()[:10]
    write_manifest()
    write_sw(version)
    write_icons()

    print(f"[build_pwa] OK -> {WEB}")
    print(f"  index.html {len(index)//1024} KB | fonts {len(font_uuids)} | "
          f"dc-script {len(patched_js)//1024} KB | vendor {len(VENDOR_LIBS)}")

    # Build the phone UI too (separate mobile.html; leaves the PC shell above untouched).
    build_mobile()


def write_manifest() -> None:
    manifest = {
        "name": "J.A.R.V.I.S", "short_name": "JARVIS",
        "description": "Aditya's personal AI assistant",
        "start_url": "/", "scope": "/", "display": "standalone",
        "orientation": "any", "background_color": "#02040a", "theme_color": "#02040a",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    (WEB / "manifest.webmanifest").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def write_sw(version: str) -> None:
    # CACHE name carries a per-build version so a rebuild publishes a new worker, which on
    # activate purges the stale shell. The app HTML is network-first (always fresh online,
    # still works offline); immutable assets (vendor/fonts/icons) are cache-first.
    sw = """// JARVIS service worker — offline shell, live API. Build: %s
const CACHE = 'jarvis-%s';
const ASSETS = [
  '/manifest.webmanifest',
  '/static/dc-runtime.js',
  '/static/vendor/react.production.min.js',
  '/static/vendor/react-dom.production.min.js',
  '/static/vendor/babel.min.js',
  '/static/icons/icon-192.png', '/static/icons/icon-512.png'
];
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  // Never touch live API/voice traffic.
  if (url.pathname === '/chat' || url.pathname === '/ticker' || url.pathname.startsWith('/memory') || url.pathname.startsWith('/voice') || url.pathname.startsWith('/admin')) return;
  // App shell (navigations + '/') -> network-first so rebuilds show up immediately.
  if (e.request.mode === 'navigate' || url.pathname === '/') {
    e.respondWith(fetch(e.request).catch(() => caches.match('/') || caches.match(e.request)));
    return;
  }
  // Immutable assets -> cache-first, populate on miss.
  e.respondWith(
    caches.match(e.request).then(hit => hit || fetch(e.request).then(res => {
      if (res.ok && url.pathname.startsWith('/static')) {
        const copy = res.clone(); caches.open(CACHE).then(c => c.put(e.request, copy));
      }
      return res;
    }))
  );
});
""" % (version, version)
    (WEB / "sw.js").write_text(sw, encoding="utf-8")


def write_icons() -> None:
    from PIL import Image, ImageDraw
    for size, maskable in [(192, False), (512, False), (512, True)]:
        img = Image.new("RGBA", (size, size), (2, 4, 10, 255))
        d = ImageDraw.Draw(img)
        c = size / 2
        pad = size * (0.16 if maskable else 0.10)   # maskable keeps content in the safe zone
        # outer faint ring
        d.ellipse([pad, pad, size - pad, size - pad], outline=(53, 231, 255, 90), width=max(2, size // 64))
        r1 = c - pad - size * 0.10
        d.ellipse([c - r1, c - r1, c + r1, c + r1], outline=(53, 231, 255, 200), width=max(3, size // 40))
        # bright core
        r2 = size * 0.13
        d.ellipse([c - r2, c - r2, c + r2, c + r2], fill=(189, 246, 255, 255))
        r3 = size * 0.20
        d.ellipse([c - r3, c - r3, c + r3, c + r3], outline=(125, 240, 255, 160), width=max(2, size // 80))
        name = f"icon-{size}{'-maskable' if maskable else ''}.png"
        img.save(ICONS / name)


if __name__ == "__main__":
    main()
