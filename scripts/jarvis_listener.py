"""
JARVIS — headless always-on voice listener (Phase 1).

Pipeline:
    mic -> openWakeWord ("hey jarvis") -> greeting
        -> [conversation] mic -> silero VAD (end-of-speech) -> Groq Whisper STT
            -> Groq LLM stream -> sentence split -> Edge-TTS -> speakers
            -> barge-in: talk over JARVIS and he stops within ~200 ms
        -> after a quiet follow-up window, drop back to wake-word watch.

Run:
    python scripts/jarvis_listener.py

Tip: use headphones (or it may hear its own voice). No GUI, no window — pure voice.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import sounddevice as sd

# Make project root importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (SAMPLE_RATE, ASSISTANT_NAME, JARVIS_USER_NAME, IDENTITY_TOKEN,  # noqa: E402
                    PROACTIVE_ENABLED, PROACTIVE_REPLY_WINDOW_S)
from app.services.llm import get_llm  # noqa: E402
from app.services.agent import get_agent  # noqa: E402
from app.services import identity  # noqa: E402  (Phase 11 — speaker recognition / access)
from app.services import runtime  # noqa: E402  (Phase 10.L — mic-mute flag from the tray)
from app.services.agent.scheduler import get_scheduler  # noqa: E402
from app.services.voice.stt import get_stt  # noqa: E402
from app.services.voice.tts import get_tts  # noqa: E402
from app.services.voice.vad import get_vad, FRAME_SAMPLES  # noqa: E402
from app.services.voice.wake_word import get_wake_word, WakeEvent  # noqa: E402
from app.services.voice.pipeline import sentence_chunks  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("jarvis.listener")

BLOCK = FRAME_SAMPLES                 # 512 samples (~32 ms) — VAD frame size
FRAME_MS = BLOCK / SAMPLE_RATE * 1000

SILENCE_HANG_MS = 700                 # end-of-speech after this much trailing silence
PREROLL_FRAMES = 13                   # ~400 ms kept before speech onset (anti word-clip)
MIN_SPEECH_MS = 250                   # ignore blips
MAX_UTTER_MS = 20000                  # safety cap on one utterance
CONVO_IDLE_S = 120.0                  # stay in conversation; only sleep after this much silence
                                      # (or when the user dismisses JARVIS — see <SLEEP>)
# Barge-in tuning — responsive enough that talking over JARVIS cuts him off quickly.
# (Tuned for headphones. On open speakers, raise these so his own voice won't self-trigger:
#  THRESHOLD~0.85, RMS_MIN~0.03, SPEECH_MS~600 — or set JARVIS_BARGEIN=0 to disable.)
BARGE_THRESHOLD = float(os.getenv("JARVIS_BARGE_THRESHOLD", "0.6"))   # VAD prob during playback
BARGE_RMS_MIN = float(os.getenv("JARVIS_BARGE_RMS", "0.015"))         # AND this loud (filters faint noise)
BARGE_SPEECH_MS = 280                 # ~2 words over him => interruption
BARGE_WARMUP_MS = 200                 # ignore the first 200 ms of playback
JARVIS_BARGEIN = os.getenv("JARVIS_BARGEIN", "1") != "0"   # set 0 to disable entirely

# Live HUD bridge — the listener POSTs its state to the backend, which fans it out to the
# PWA so the on-screen orb/transcript/reply track the voice in real time. Best-effort: if
# the backend isn't up, voice still works; the HUD just won't mirror.
EVENTS_URL = os.getenv("JARVIS_EVENTS_URL", "http://127.0.0.1:8000/events/publish")
# The backend is the single brain — the listener delegates thinking here (so memory/tools
# load once, in the backend). If the backend is down, it falls back to a local agent.
CHAT_URL = os.getenv("JARVIS_CHAT_URL", "http://127.0.0.1:8000/chat")
CHAT_STREAM_URL = os.getenv("JARVIS_CHAT_STREAM_URL", "http://127.0.0.1:8000/chat/stream")
# Phase 5 — speech-emotion (voice tone). The listener POSTs the utterance WAV here IN PARALLEL
# with the STT call, so JARVIS reads how it was said as well as what was said, at ~no extra cost.
EMOTION_URL = os.getenv("JARVIS_EMOTION_URL", "http://127.0.0.1:8000/voice/emotion")
# Phase 7 — proactive message announcements. The backend buffers spoken lines for new
# WhatsApp/Instagram/email messages; the listener drains + speaks them between turns.
ANNOUNCE_URL = os.getenv("JARVIS_ANNOUNCE_URL", "http://127.0.0.1:8000/messaging/announcements")
ANNOUNCE_POLL_S = float(os.getenv("JARVIS_ANNOUNCE_POLL_S", "8"))
# Phase 8 — call announcements. The backend buffers a spoken line when the phone rings or a
# call is missed (the Android companion pushes the event); the listener speaks it between turns.
# Polled fast so a ring is announced while it's still ringing.
CALLS_ANNOUNCE_URL = os.getenv("JARVIS_CALLS_ANNOUNCE_URL", "http://127.0.0.1:8000/calls/announcements")
CALLS_POLL_S = float(os.getenv("JARVIS_CALLS_POLL_S", "3"))
# Phase 11 — identity. The backend owns the camera, so the listener asks IT to scan a face
# (startup gate + guided-enrolment face capture). Voice ID is done locally in the listener.
_BACKEND = os.getenv("JARVIS_BACKEND", "http://127.0.0.1:8000")
SCAN_URL = f"{_BACKEND}/identity/scan"
ENROLL_PENDING_URL = f"{_BACKEND}/identity/enroll/pending"
ENROLL_FACE_URL = f"{_BACKEND}/identity/enroll/face"
ENROLL_VOICE_URL = f"{_BACKEND}/identity/enroll/voice"
ENROLL_FINALIZE_URL = f"{_BACKEND}/identity/enroll/finalize"
# Where a hands-free spoken call command ("decline") is sent. After announcing an INCOMING call,
# the listener opens a brief window where the NEXT thing the boss says is treated as the call
# command — no "wake up jarvis" needed (the ring is the wake).
CALL_COMMAND_URL = os.getenv("JARVIS_CALL_COMMAND_URL", "http://127.0.0.1:8000/calls/command")
RING_VOICE_WINDOW_S = float(os.getenv("JARVIS_RING_VOICE_WINDOW_S", "20"))

# Phase 10.F — proactive. The listener polls the backend with its state (in a conversation? idle how
# long?); the backend decides whether/what JARVIS should say on his own and returns the line. The
# listener speaks it and, for a nudge fired outside a conversation, opens a brief reply window so the
# boss can answer without saying "wake up jarvis".
PROACTIVE_URL = os.getenv("JARVIS_PROACTIVE_URL", "http://127.0.0.1:8000/proactive/poll")
ACK_URL = os.getenv("JARVIS_PROACTIVE_ACK_URL", "http://127.0.0.1:8000/proactive/ack")
PROACTIVE_POLL_S = float(os.getenv("JARVIS_PROACTIVE_POLL_S", "25"))
# Phase 10.B — intel feed anomaly alerts. The backend monitor buffers a spoken line when something
# the boss watches moves enough (a price swing, a quake near family); the listener drains + speaks it.
FEEDS_ALERTS_URL = os.getenv("JARVIS_FEEDS_ALERTS_URL", "http://127.0.0.1:8000/feeds/alerts")
FEEDS_ALERTS_POLL_S = float(os.getenv("JARVIS_FEEDS_ALERTS_POLL_S", "30"))
# Phase 10.A — deep research. A sweep runs in the background; the listener drains live progress lines
# (spoken plainly, mid-work) and finished-briefing announcements (spoken with a soft chime). Polled
# faster than feeds so a "ready" line lands promptly after a sweep finishes.
RESEARCH_PROGRESS_URL = os.getenv("JARVIS_RESEARCH_PROGRESS_URL", "http://127.0.0.1:8000/research/progress")
RESEARCH_DONE_URL = os.getenv("JARVIS_RESEARCH_DONE_URL", "http://127.0.0.1:8000/research/done")
RESEARCH_POLL_S = float(os.getenv("JARVIS_RESEARCH_POLL_S", "12"))

# Sentinel: wait_for_wake returns this (instead of a WakeEvent) when a call is ringing, so the
# run loop knows to capture a call command rather than greet + open a normal conversation.
_CALL_WAKE = object()
# Sentinel: a proactive nudge (spoken outside a conversation) opened a reply window — the run loop
# captures the boss's answer as a normal turn, no wake word needed.
_PROACTIVE_WAKE = object()


def _alarm_tone(repeats: int = 3, sr: int = SAMPLE_RATE) -> np.ndarray:
    """A short two-tone alarm beep (880/660 Hz), repeated."""
    t = np.linspace(0, 0.22, int(sr * 0.22), endpoint=False)
    hi = (0.5 * np.sin(2 * np.pi * 880 * t)).astype(np.float32)
    lo = (0.5 * np.sin(2 * np.pi * 660 * t)).astype(np.float32)
    gap = np.zeros(int(sr * 0.10), dtype=np.float32)
    one = np.concatenate([hi, gap, lo, gap])
    return np.tile(one, repeats)


class Listener:
    def __init__(self) -> None:
        logger.info("Booting %s voice listener — loading models...", ASSISTANT_NAME)
        self.wake = get_wake_word()
        self.vad = get_vad()
        self.stt = get_stt()
        self.tts = get_tts()
        self.llm = get_llm()
        self._agent = None                # lazy: only built if the backend is unreachable
        self.history: list[dict] = []     # used only by the local fallback agent
        self._http = httpx.AsyncClient()
        self._speaking_lock = asyncio.Lock()
        # HUD events are drained by ONE sender task in order. Fire-and-forget create_task per
        # event raced over HTTP and could deliver e.g. `listening` before the preceding `idle`,
        # leaving the HUD stuck on "AWAITING WAKE UP" while JARVIS was actually listening.
        self._emit_q: asyncio.Queue = asyncio.Queue()
        self._barge_frames: list[np.ndarray] | None = None   # speech that interrupted JARVIS
        # Phase 8: while > now (monotonic), a call is ringing and the next utterance is a call
        # command (decline/answer/silence) — set by the call-announce loop, consumed by wait_for_wake.
        self._ring_window_until = 0.0
        self._muted = runtime.is_muted()  # Phase 10.L — tray/always-on mic mute (checked in wait_for_wake)
        # Phase 10.F — proactive state the engine reads via the poll loop.
        self._in_conversation = False                # True while an open conversation turn-loop runs
        self._last_interaction_ts = time.time()      # wall-clock of the last user turn / wake
        self._proactive_window_until = 0.0           # monotonic; a nudge opened a brief reply window
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=BLOCK)
        logger.info("Models loaded. %s is listening for the wake word.", ASSISTANT_NAME)

    # -- live HUD bridge: enqueue (non-blocking) so the PWA orb tracks the voice in ORDER --
    def _emit(self, **event) -> None:
        self._emit_q.put_nowait(event)

    async def _emit_sender(self) -> None:
        """Drain HUD events strictly in the order they were produced (one POST at a time), so
        the PWA never sees a stale state win a race (e.g. idle landing after a later listening)."""
        while True:
            event = await self._emit_q.get()
            await self._publish(event)

    async def _publish(self, event: dict) -> None:
        try:
            await self._http.post(EVENTS_URL, json=event, timeout=1.5)
        except Exception:  # noqa: BLE001
            pass

    async def _narrate(self, line: str) -> None:
        """Speak a short mid-task status aloud (e.g. 'Searching the web.') while a tool runs."""
        line = line if line.endswith((".", "…", "!", "?")) else line + "."
        pcm, sr = await self.tts.synthesize_pcm(line)
        if pcm.size:
            await self._play_with_barge(pcm, sr)

    async def _voice_emotion(self, pcm_bytes: bytes) -> dict | None:
        """Speech-emotion on the just-recorded audio. Runs CONCURRENTLY with STT (both only need
        the audio), so it adds ~no latency. Best-effort — returns None if the backend/model is off."""
        if not pcm_bytes:
            return None
        try:
            from app.services.voice.stt import pcm_to_wav_bytes
            wav = pcm_to_wav_bytes(np.frombuffer(pcm_bytes, dtype=np.int16))
            r = await self._http.post(EMOTION_URL, files={"file": ("u.wav", wav, "audio/wav")}, timeout=8)
            if r.status_code == 200:
                d = r.json()
                return d or None
        except Exception:  # noqa: BLE001
            pass
        return None

    # -- Phase 11: who's speaking? Runs CONCURRENTLY with STT (both only need the audio), so the
    # voiceprint (~30-50 ms) hides under the STT round-trip and adds ~no latency. --
    def _identify(self, pcm_np: np.ndarray):
        try:
            return identity.resolve("pc_voice", voice=pcm_np, sr=SAMPLE_RATE)
        except Exception:  # noqa: BLE001
            return None

    def _warm_identity(self) -> None:
        """Pre-load the voice encoder once at startup so the first command isn't slowed."""
        try:
            if identity.enabled() and identity.is_enrolled():
                identity.identify_voice(np.zeros(SAMPLE_RATE, dtype=np.int16))
        except Exception:  # noqa: BLE001
            pass

    async def _deflect_stranger(self) -> None:
        """An unrecognised voice — decline in character and don't engage the brain at all."""
        line = (f"You're a stranger to me — not on {JARVIS_USER_NAME} sir's list of trusted "
                f"people — so I can't answer you.")
        self._emit(type="transcript", text="(unrecognised voice)")
        self._emit(type="reply", text=line)
        await self._say(line)

    async def _say(self, line: str) -> None:
        """Speak one line, blocking until done (used by the startup gate + enrolment dialog)."""
        try:
            pcm, sr = await self.tts.synthesize_pcm(line)
            if pcm.size:
                await self._play_blocking(pcm, sr)
        except Exception:  # noqa: BLE001
            pass

    # -- Phase 11: startup face gate — identify who's at the camera and greet them by name. --
    async def _startup_face_gate(self) -> None:
        if not identity.enabled() or not identity.is_enrolled():
            return                      # open mode: nothing enrolled, behave as before
        res = None
        for _ in range(4):              # the backend may still be booting
            try:
                r = await self._http.post(SCAN_URL, headers={"x-jarvis-token": IDENTITY_TOKEN}, timeout=20)
                if r.status_code == 200:
                    res = r.json(); break
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(2)
        if not res:
            return
        tier = res.get("tier"); name = (res.get("active") or {}).get("name") or res.get("display") or ""
        if tier == "owner":
            await self._say("Face recognised. Welcome back, sir.")
        elif tier in ("trusted", "guest"):
            await self._say(f"Face recognised. Hello, {name}.")
        elif tier == "stranger":
            await self._say(f"I don't recognise you — you're a stranger to me, not on "
                            f"{JARVIS_USER_NAME} sir's list of trusted people.")
        # tier == 'unsure'/no camera -> say nothing, fall back to per-turn voice verification

    # -- Phase 11: guided enrolment — after the Owner says "add X as trusted", capture their
    # face + voice over a few turns. Driven by the backend's pending session. --
    async def _maybe_run_enrollment(self) -> None:
        try:
            r = await self._http.get(ENROLL_PENDING_URL, headers={"x-jarvis-token": IDENTITY_TOKEN}, timeout=4)
            p = r.json() if r.status_code == 200 else {}
        except Exception:  # noqa: BLE001
            return
        if not p or not p.get("name"):
            return
        from app.services.voice.stt import pcm_to_wav_bytes
        name, sentences = p["name"], (p.get("sentences") or ["Please read this line."])
        hdr = {"x-jarvis-token": IDENTITY_TOKEN}
        await self._say(f"{name}, look straight at the camera for me.")
        try:
            await self._http.post(ENROLL_FACE_URL, headers=hdr, timeout=20)
        except Exception:  # noqa: BLE001
            pass
        need = int(p.get("need_voice", 3))
        for i in range(need):
            await self._say(f"Now read this, please: {sentences[i % len(sentences)]}")
            pcm = await self.record_utterance(max_wait_s=9)
            if not pcm:
                await self._say("I didn't catch that — once more.")
                pcm = await self.record_utterance(max_wait_s=9)
            if pcm:
                wav = pcm_to_wav_bytes(np.frombuffer(pcm, dtype=np.int16))
                try:
                    await self._http.post(ENROLL_VOICE_URL, headers=hdr,
                                          files={"file": ("u.wav", wav, "audio/wav")}, timeout=10)
                except Exception:  # noqa: BLE001
                    pass
        try:
            r = await self._http.post(ENROLL_FINALIZE_URL, headers=hdr, timeout=10)
            msg = (r.json().get("message") if r.status_code == 200 else None) or "Enrolment complete, sir."
        except Exception:  # noqa: BLE001
            msg = "Enrolment finished, sir."
        await self._say(msg)

    async def _think(self, user_text: str, voice_emotion: dict | None = None,
                     trust=None) -> tuple[str, bool, dict | None]:
        """Ask the backend brain (single source of memory/tools), streaming so tool-status
        narration is spoken aloud as it happens. Falls back to a local agent if the backend
        isn't reachable, so a standalone listener still works. `trust` is the Phase-11 verified
        speaker — its tier/name ride along so the backend gates tools to who's actually talking."""
        try:
            reply_text, sleep, prosody = "", False, None
            payload = {"text": user_text, "session_id": "pc_voice", "channel": "pc_voice"}
            if voice_emotion:
                payload["voice_emotion"] = voice_emotion
            if trust is not None:
                payload["speaker_tier"] = trust.tier
                payload["speaker_name"] = trust.display or trust.name
            async with self._http.stream("POST", CHAT_STREAM_URL, timeout=120, json=payload) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") == "narrate" and ev.get("text"):
                        await self._narrate(ev["text"])      # speak "Searching the web." now
                    elif ev.get("type") == "reply":
                        reply_text = (ev.get("reply") or "").strip()
                        sleep = bool(ev.get("sleep"))
                        prosody = ev.get("prosody")          # Phase 5: per-register voice rate/pitch
            return reply_text, sleep, prosody
        except Exception as e:  # noqa: BLE001
            logger.warning("backend /chat unavailable (%s) — thinking locally", type(e).__name__)
            if self._agent is None:
                self._agent = get_agent()
            reply = await self._agent.run(user_text, history=self.history,
                                          narrate=self._narrate, channel="pc_voice",
                                          voice_emotion=voice_emotion, trust=trust)
            full = (reply.text or "").strip()
            if full:
                self.history.append({"role": "user", "content": user_text})
                self.history.append({"role": "assistant", "content": full})
                self.history[:] = self.history[-20:]
            prosody = (reply.mood or {}).get("prosody")
            return full, reply.sleep, prosody

    # -- low-level mic read (blocking) bridged to async --
    async def _read(self) -> np.ndarray:
        data, _ = await asyncio.to_thread(self.stream.read, BLOCK)
        return data.reshape(-1).astype(np.int16)

    # ------------------------------------------------------------------ #
    async def wait_for_wake(self):
        self.wake.reset()
        self._muted = runtime.is_muted()
        _mute_tick = 0
        while True:
            block = await self._read()
            # Mic-mute (Phase 10.L — tray / always-on). When muted, ignore the wake word AND the
            # call-wake entirely: keep draining the mic so we stay its sole reader, but never hand
            # control to the brain. The flag is a cheap file-stat, throttled to ~0.5 s.
            _mute_tick += 1
            if _mute_tick >= 15:
                _mute_tick = 0
                self._muted = runtime.is_muted()
            if self._muted:
                continue
            # An incoming call short-circuits the wake word — the ring IS the wake, so the boss can
            # just say "decline" / "answer". Keep reading the mic (we stay the sole reader) so the
            # Vosk grammar never sees these frames; the run loop captures the command next.
            if time.monotonic() < self._ring_window_until:
                return _CALL_WAKE
            # A proactive nudge just spoke and opened a reply window — capture his answer as a turn.
            if time.monotonic() < self._proactive_window_until:
                return _PROACTIVE_WAKE
            ev = self.wake.process(block)
            if ev is not None:
                return ev

    # Record one utterance using VAD. Returns int16 PCM bytes, or None on timeout.
    async def record_utterance(self, max_wait_s: float) -> bytes | None:
        self.vad.reset()
        utter = bytearray()
        speech_ms = 0.0
        silence_ms = 0.0
        in_speech = False
        started = time.monotonic()
        # Pre-roll: keep the last ~400 ms of audio so the onset of the first word is never
        # clipped when the VAD trips a frame or two late. This is the main accuracy win.
        preroll = collections.deque(maxlen=PREROLL_FRAMES)

        # If JARVIS was just interrupted, seed this utterance with the words that cut him off
        # so they're transcribed as the command (instead of being lost in the handoff).
        if self._barge_frames:
            for f in self._barge_frames:
                utter.extend(f.tobytes())
            speech_ms = len(self._barge_frames) * FRAME_MS
            in_speech = True
            self._barge_frames = None

        while True:
            block = await self._read()
            elapsed = time.monotonic() - started
            framef = block.astype(np.float32) / 32768.0
            is_sp = self.vad.is_speech(framef)

            if not in_speech:
                if is_sp:
                    in_speech = True
                    speech_ms = FRAME_MS
                    for pb in preroll:                # prepend buffered onset
                        utter.extend(pb.tobytes())
                    utter.extend(block.tobytes())
                else:
                    preroll.append(block)
                    if elapsed >= max_wait_s:
                        return None                   # nobody spoke
                continue

            # in speech
            utter.extend(block.tobytes())
            if is_sp:
                speech_ms += FRAME_MS
                silence_ms = 0.0
            else:
                silence_ms += FRAME_MS
                if silence_ms >= SILENCE_HANG_MS:
                    break
            if speech_ms + silence_ms >= MAX_UTTER_MS:
                break

        if speech_ms < MIN_SPEECH_MS:
            # Spoke too briefly (a cough, a stray noise, the tail of JARVIS's own line) — that is
            # NOT the boss going quiet, so return b"" to keep the conversation open and listen
            # again, rather than None (which means "real silence, drop to wake-word watch").
            return b""
        return bytes(utter)

    # Stream LLM -> sentences -> TTS -> speakers, with barge-in.
    # Run the tool-using agent on the user's request, narrating short status aloud while
    # tools run, then speak the final answer. Returns True if the user dismissed JARVIS
    # (so the caller drops back to wake-word watch).
    async def speak_response(self, user_text: str, voice_emotion: dict | None = None,
                             trust=None) -> bool:
        self._emit(type="transcript", text=user_text)
        reply_text, sleep, prosody = await self._think(user_text, voice_emotion, trust)  # backend brain
        self._emit(type="reply", text=reply_text)

        async def _one():
            yield reply_text

        # Hold the SAME mutex the background announce loops (messages/calls/feeds/research/proactive)
        # use, so a backgrounded line can never play over — or chop off — a live reply. Only the
        # SPEAKING is guarded (not _think), so alerts queue behind the reply rather than during it.
        async with self._speaking_lock:
            await self._speak_sentences(sentence_chunks(_one()), prosody)
        # NOTE: no `idle` here. While staying in open conversation JARVIS goes straight back to
        # listening for the next turn (the run loop emits `listening`); `idle` is owned by the
        # run loop and fired ONLY when actually returning to wake-word watch. Emitting it here
        # caused a speaking->idle->listening flicker that often stuck the HUD on standby.
        return sleep

    # Prefetch pipeline: a PRODUCER synthesizes each sentence ahead into a small queue; the
    # CONSUMER plays them back-to-back. So while JARVIS speaks sentence N, sentence N+1's
    # audio is already made — no gaps, and he starts the moment sentence 1 is ready.
    async def _speak_sentences(self, sentence_aiter, prosody: dict | None = None) -> bool:
        queue: asyncio.Queue = asyncio.Queue(maxsize=3)
        _DONE = object()
        rate = (prosody or {}).get("rate")     # Phase 5: colour JARVIS's voice to the register
        pitch = (prosody or {}).get("pitch")

        async def producer() -> None:
            try:
                async for sentence in sentence_aiter:
                    pcm, sr = await self.tts.synthesize_pcm(sentence, rate, pitch)
                    if pcm.size:
                        await queue.put((sentence, pcm, sr))
            except Exception:  # noqa: BLE001
                logger.exception("synthesis failed")
            finally:
                await queue.put(_DONE)

        prod = asyncio.create_task(producer())
        interrupted = False
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    break
                sentence, pcm, sr = item
                logger.info("JARVIS: %s", sentence)
                if await self._play_with_barge(pcm, sr):
                    interrupted = True
                    break
        finally:
            prod.cancel()
        return interrupted

    # Play PCM while watching the mic. On a real, sustained interruption, stop and KEEP the
    # interrupting speech (in self._barge_frames) so it becomes the next command — no gap.
    async def _play_with_barge(self, pcm: np.ndarray, sr: int) -> bool:
        sd.play(pcm, sr)
        # Barge-in disabled: just drain the mic so it doesn't overflow, and play through.
        if not JARVIS_BARGEIN:
            while sd.get_stream().active:
                await self._read()
            return False

        speech_ms = 0.0
        played_ms = 0.0
        barge_frames: list[np.ndarray] = []
        while sd.get_stream().active:
            block = await self._read()
            played_ms += FRAME_MS
            if played_ms < BARGE_WARMUP_MS:        # let playback settle first
                continue
            framef = block.astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(framef ** 2)))
            # Require BOTH a confident speech prob AND real loudness, sustained — so his own
            # voice / faint noise won't trip it, but you talking over him will.
            if self.vad.speech_prob(framef) >= BARGE_THRESHOLD and rms >= BARGE_RMS_MIN:
                speech_ms += FRAME_MS
                barge_frames.append(block)
                if speech_ms >= BARGE_SPEECH_MS:
                    sd.stop()
                    self.vad.reset()
                    self._barge_frames = barge_frames   # carry the interrupting words forward
                    logger.info("Barge-in — JARVIS yields.")
                    return True
            else:
                speech_ms = 0.0
                barge_frames = []
        return False

    async def greet(self) -> None:
        # One short spoken greeting on wake (kept tiny for snappy feel) — by the name of whoever
        # is the active/recognised user (Phase 11).
        ctx = "(The user just said the wake word with nothing else. Greet briefly in one short line.)"
        if identity.enabled():
            av = identity.active_view()
            if av.get("tier") == "owner":
                ctx = "(The Owner woke you with no command. Greet him briefly, addressing him as 'sir'.)"
            elif av.get("tier") in ("trusted", "guest"):
                ctx = (f"(The user {av.get('name')} ({av.get('tier')} access) woke you with no "
                       f"command. Greet them briefly BY NAME, warmly but knowing they're not the Owner.)")
        line = await self.llm.chat(ctx, history=self.history)
        line = line or "At your service, sir."
        logger.info("JARVIS: %s", line)
        pcm, sr = await self.tts.synthesize_pcm(line)
        if pcm.size:
            async with self._speaking_lock:   # same mutex as the announce loops — never overlap
                await self._play_with_barge(pcm, sr)

    # Speak a reminder/timer aloud when it fires, preceded by an audible alarm chime
    # (played through JARVIS's own output so it rings even if Windows toast audio is muted).
    async def _announce_reminder(self, r) -> None:
        if r.kind == "timer":
            line = f"Sir, your timer's up. {r.text}" if r.text and r.text != "Timer's up." else "Sir, your timer's up."
        else:
            line = f"A reminder, sir: {r.text}"
        async with self._speaking_lock:
            logger.info("JARVIS (reminder): %s", line)
            await self._play_blocking(_alarm_tone(), SAMPLE_RATE)        # ring first
            pcm, sr = await self.tts.synthesize_pcm(line)                # then announce
            if pcm.size:
                await self._play_blocking(pcm, sr)

    async def _play_blocking(self, audio: np.ndarray, sr: int) -> None:
        sd.play(audio, sr)
        while sd.get_stream().active:
            await asyncio.sleep(0.05)

    # Phase 7 — proactively speak new-message alerts (WhatsApp/Instagram/email). Polls the
    # backend's announcement buffer between turns; uses the speaking lock so it never overlaps
    # a reminder or another announcement. A soft double-chime precedes the line.
    async def _announce_messages_loop(self) -> None:
        while True:
            try:
                r = await self._http.get(ANNOUNCE_URL, timeout=4)
                lines = r.json().get("lines", []) if r.status_code == 200 else []
            except Exception:  # noqa: BLE001
                lines = []
            for line in lines:
                async with self._speaking_lock:
                    logger.info("JARVIS (message): %s", line)
                    self._emit(type="reply", text=line)
                    await self._play_blocking(_alarm_tone(repeats=1), SAMPLE_RATE)
                    pcm, sr = await self.tts.synthesize_pcm(line)
                    if pcm.size:
                        await self._play_blocking(pcm, sr)
            await asyncio.sleep(ANNOUNCE_POLL_S)

    # Phase 8 — speak phone-call alerts (incoming ring / missed call). Polled fast so a ring is
    # announced while it's still ringing; he then acts by voice ("wake up jarvis, decline") or by
    # tapping the PWA call card. Uses the speaking lock so it never overlaps another announcement.
    async def _announce_calls_loop(self) -> None:
        while True:
            try:
                r = await self._http.get(CALLS_ANNOUNCE_URL, timeout=4)
                items = r.json().get("items", []) if r.status_code == 200 else []
            except Exception:  # noqa: BLE001
                items = []
            for item in items:
                line = item.get("line", "") if isinstance(item, dict) else str(item)
                kind = item.get("kind", "") if isinstance(item, dict) else ""
                if not line:
                    continue
                # An incoming RING opens the hands-free window FIRST (before we speak), so the run
                # loop's wait_for_wake breaks out and then blocks on the speaking lock — it captures
                # the boss's "decline"/"answer" the instant the announcement finishes. A missed-call
                # line just gets spoken.
                if kind == "incoming":
                    self._ring_window_until = time.monotonic() + RING_VOICE_WINDOW_S
                async with self._speaking_lock:
                    logger.info("JARVIS (call): %s", line)
                    self._emit(type="reply", text=line)
                    await self._play_blocking(_alarm_tone(repeats=2), SAMPLE_RATE)
                    pcm, sr = await self.tts.synthesize_pcm(line)
                    if pcm.size:
                        await self._play_blocking(pcm, sr)
            await asyncio.sleep(CALLS_POLL_S)

    # Phase 10.B — speak intel feed alerts (a watched price swung, a quake near family). Polled like
    # the message/call announce loops; uses the speaking lock so it never overlaps another line. A
    # soft double-chime precedes it. The backend already held non-critical alerts during quiet hours.
    async def _feeds_alerts_loop(self) -> None:
        while True:
            await asyncio.sleep(FEEDS_ALERTS_POLL_S)
            try:
                r = await self._http.get(FEEDS_ALERTS_URL, timeout=4)
                lines = r.json().get("lines", []) if r.status_code == 200 else []
            except Exception:  # noqa: BLE001
                continue
            for line in lines:
                try:
                    async with self._speaking_lock:
                        logger.info("JARVIS (intel): %s", line)
                        self._emit(type="reply", text=line)
                        await self._play_blocking(_alarm_tone(repeats=1), SAMPLE_RATE)
                        pcm, sr = await self.tts.synthesize_pcm(line)
                        if pcm.size:
                            await self._play_blocking(pcm, sr)
                except Exception:  # noqa: BLE001 — one TTS hiccup must not kill the loop
                    logger.exception("feed alert speak failed")

    # Phase 10.A — deep research. While a sweep runs in the background, the backend buffers short
    # progress lines and, when it finishes, the briefing announcement. The listener drains both and
    # speaks them (progress plainly mid-work; the finished briefing with a soft chime) — so JARVIS
    # narrates the dig and comes back with the result while the boss carries on with other things.
    async def _research_loop(self) -> None:
        while True:
            await asyncio.sleep(RESEARCH_POLL_S)
            # Progress: "still working" lines — spoken plainly, no chime.
            try:
                r = await self._http.get(RESEARCH_PROGRESS_URL, timeout=4)
                prog = r.json().get("lines", []) if r.status_code == 200 else []
            except Exception:  # noqa: BLE001
                prog = []
            for line in prog:
                try:
                    async with self._speaking_lock:
                        logger.info("JARVIS (research): %s", line)
                        self._emit(type="reply", text=line)
                        pcm, sr = await self.tts.synthesize_pcm(line)
                        if pcm.size:
                            await self._play_blocking(pcm, sr)
                except Exception:  # noqa: BLE001 — a TTS hiccup must not kill the loop
                    logger.exception("research progress speak failed")
            # Done: a finished briefing (or a monitored-topic development) — soft chime, then speak.
            try:
                r = await self._http.get(RESEARCH_DONE_URL, timeout=4)
                items = r.json().get("items", []) if r.status_code == 200 else []
            except Exception:  # noqa: BLE001
                items = []
            for item in items:
                line = (item or {}).get("speak", "")
                if not line:
                    continue
                try:
                    async with self._speaking_lock:
                        logger.info("JARVIS (research/done): %s", line)
                        self._emit(type="reply", text=line)
                        await self._play_blocking(_alarm_tone(repeats=1), SAMPLE_RATE)
                        pcm, sr = await self.tts.synthesize_pcm(line)
                        if pcm.size:
                            await self._play_blocking(pcm, sr)
                except Exception:  # noqa: BLE001
                    logger.exception("research done speak failed")

    # Phase 10.F — proactive. Tell the backend our state; it decides whether JARVIS should speak up
    # on his own (a routine nudge, a check-in, a quiet remark, a hydration prompt, "you haven't
    # called Mom in a while"). If it returns a line, speak it (speaking lock => never overlaps), and
    # for a nudge fired OUTSIDE a conversation open a short reply window so he can answer hands-free.
    async def _proactive_loop(self) -> None:
        if not PROACTIVE_ENABLED:
            return
        while True:
            await asyncio.sleep(PROACTIVE_POLL_S)
            try:
                # Don't self-initiate while a call is live or a reply window is already open.
                now_m = time.monotonic()
                if now_m < self._ring_window_until or now_m < self._proactive_window_until:
                    continue
                state = {
                    "in_conversation": self._in_conversation,
                    "idle_s": max(0.0, time.time() - self._last_interaction_ts),
                    "channel": "pc_voice",
                }
                try:
                    r = await self._http.post(PROACTIVE_URL, json=state, timeout=12)
                    data = r.json() if r.status_code == 200 else {}
                except Exception:  # noqa: BLE001
                    continue
                say = (data or {}).get("say")
                kind = (data or {}).get("kind")
                if not say:
                    continue
                # While a conversation is open, the only nudge that may interject is idle-chatter
                # (which fires in a genuine lull). Any other (routine/call/hydration) would collide
                # with the live reply's audio — defer it: don't speak, don't ack, so it re-fires once
                # the conversation ends (the line was NOT counted, since counting happens on ack).
                if self._in_conversation and kind != "idle":
                    continue
                async with self._speaking_lock:
                    logger.info("JARVIS (proactive/%s): %s", kind, say)
                    self._emit(type="reply", text=say)
                    pcm, sr = await self.tts.synthesize_pcm(say)   # no chime — it's conversational
                    if pcm.size:
                        await self._play_blocking(pcm, sr)
                # Spoken successfully -> confirm so it counts toward the cap/min-gap/dedup.
                try:
                    await self._http.post(ACK_URL, json={"kind": kind, "key": (data or {}).get("key", "")},
                                          timeout=4)
                except Exception:  # noqa: BLE001
                    pass
                # If he's expected to reply and we're not already mid-conversation, open a brief
                # window so wait_for_wake hands his answer straight to the brain (no "wake up jarvis").
                if data.get("expects_reply") and not self._in_conversation:
                    self._proactive_window_until = time.monotonic() + PROACTIVE_REPLY_WINDOW_S
            except Exception:  # noqa: BLE001 — one bad turn must never kill the loop (TTS/audio hiccup)
                logger.exception("proactive loop iteration failed")

    # Phase 8 — a call is ringing and the boss just spoke: treat it as a hands-free call command
    # (decline/answer/silence) with NO wake word. Reuses the backend's command normalisation; if
    # what he said isn't a call command, it falls through to the normal brain ("who is it?").
    async def _handle_ring_voice(self) -> None:
        self._ring_window_until = 0.0                 # consume — one capture per ring
        async with self._speaking_lock:               # wait until the announcement finishes speaking
            pass
        self._emit(type="listening")
        pcm = await self.record_utterance(max_wait_s=7)
        if not pcm:
            self._emit(type="idle")
            return
        text = (await asyncio.to_thread(self.stt.transcribe, np.frombuffer(pcm, dtype=np.int16)) or "").strip()
        if not text:
            self._emit(type="idle")
            return
        logger.info("You (call): %s", text)
        try:
            r = await self._http.post(CALL_COMMAND_URL, json={"action": text}, timeout=4)
            res = r.json() if r.status_code == 200 else {}
        except Exception:  # noqa: BLE001
            res = {}
        if res.get("ok"):                             # recognised call command — confirm aloud
            line = res.get("message", "")
            if line:
                async with self._speaking_lock:
                    self._emit(type="reply", text=line)
                    pcm2, sr2 = await self.tts.synthesize_pcm(line)
                    if pcm2.size:
                        await self._play_blocking(pcm2, sr2)
            self._emit(type="idle")
        else:                                         # not a call command — let the normal brain answer
            await self.speak_response(text)
            self._emit(type="idle")

    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        self.stream.start()
        sched = get_scheduler()
        sched.on_fire(lambda r: asyncio.create_task(self._announce_reminder(r)))
        sched.start()
        announce_task = asyncio.create_task(self._announce_messages_loop())  # Phase 7 alerts
        calls_task = asyncio.create_task(self._announce_calls_loop())        # Phase 8 call alerts
        feeds_task = asyncio.create_task(self._feeds_alerts_loop())          # Phase 10.B — intel alerts
        research_task = asyncio.create_task(self._research_loop())           # Phase 10.A — deep-research updates
        proactive_task = asyncio.create_task(self._proactive_loop())         # Phase 10.F — self-initiated
        emit_task = asyncio.create_task(self._emit_sender())                 # ordered HUD bridge
        asyncio.create_task(asyncio.to_thread(self._warm_identity))          # Phase 11 — preload voice ID
        await self._startup_face_gate()      # Phase 11 — mandatory face check at startup, then voice
        try:
            while True:
                ev = await self.wait_for_wake()
                if ev is _CALL_WAKE:            # a call is ringing — capture "decline"/"answer" hands-free
                    await self._handle_ring_voice()
                    continue
                self._emit(type="listening")    # woke -> HUD goes attentive
                self._last_interaction_ts = time.time()
                asleep = False
                if ev is _PROACTIVE_WAKE:
                    # A proactive nudge spoke and opened a reply window — let it finish, then his
                    # next words are captured as a normal turn (no greet, no command parse).
                    self._proactive_window_until = 0.0
                    async with self._speaking_lock:
                        pass
                elif ev.command:
                    # One-breath: "wake up jarvis, <command>" -> answer the command straight away.
                    logger.info("You: %s", ev.command)
                    asleep = await self.speak_response(ev.command)
                    await self._maybe_run_enrollment()    # "add X as trusted" -> capture face+voice
                else:
                    await self.greet()
                # Stay in open conversation: keep taking turns until the user dismisses
                # JARVIS ("goodbye", "go to sleep", ...) or a long idle silence passes. While here,
                # _in_conversation lets the proactive loop drop an earned idle-chatter line in a lull.
                self._in_conversation = True
                while not asleep:
                    self._emit(type="listening")    # ready for the next turn
                    pcm = await self.record_utterance(max_wait_s=CONVO_IDLE_S)
                    if pcm is None:
                        logger.info("Idle — back to wake-word watch.")
                        break
                    if not pcm:
                        continue        # too-brief blip — stay in conversation, listen again
                    pcm_np = np.frombuffer(pcm, dtype=np.int16)
                    # Transcribe, read the voice tone, AND verify WHO is speaking — all in parallel,
                    # all only need the audio, so identity + emotion hide under the STT round-trip
                    # (no added latency).
                    text, voice_emo, trust = await asyncio.gather(
                        asyncio.to_thread(self.stt.transcribe, pcm_np),
                        self._voice_emotion(pcm),
                        asyncio.to_thread(self._identify, pcm_np),
                    )
                    text = (text or "").strip()
                    if not text:
                        continue
                    self._last_interaction_ts = time.time()   # 10.F — resets the idle/lull clock
                    # Phase 11 — an unknown voice gets a polite deflection, never the brain.
                    if trust is not None and trust.is_stranger:
                        logger.info("Unrecognised voice — deflecting.")
                        await self._deflect_stranger()
                        continue
                    who = (trust.display or trust.name) if trust else ""
                    logger.info("You%s: %s", f" ({who})" if who else "", text)
                    asleep = await self.speak_response(text, voice_emo, trust)
                    await self._maybe_run_enrollment()    # "add X as trusted" -> capture face+voice
                    if asleep:
                        logger.info("Dismissed — back to wake-word watch.")
                # Only now — genuinely leaving the conversation — does the HUD drop to standby.
                self._in_conversation = False
                self._emit(type="idle")
        finally:
            announce_task.cancel()
            calls_task.cancel()
            feeds_task.cancel()
            research_task.cancel()
            proactive_task.cancel()
            emit_task.cancel()
            self.stream.stop()
            self.stream.close()


def main() -> None:
    try:
        asyncio.run(Listener().run())
    except KeyboardInterrupt:
        print("\nJARVIS listener stopped.")


if __name__ == "__main__":
    main()
