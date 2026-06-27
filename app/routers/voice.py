"""
Voice endpoints.

  POST /voice/stt          multipart audio file -> {"text": ...}
  POST /voice/tts/stream   {"text": ...} -> streaming audio/mpeg (MP3)
  WS   /voice/converse     full-duplex: client streams mic PCM; server streams back
                           transcript, reply text, and JARVIS's spoken audio — with
                           barge-in (user speaking cancels JARVIS mid-sentence).

WS protocol (client -> server):
  - binary frames: raw 16 kHz mono int16 PCM (mic)
  - text JSON: {"type":"reset"}  clear conversation history
Server -> client:
  - text JSON: {"type":"listening"} | {"type":"transcript","text":..} |
               {"type":"reply","text":..} | {"type":"speaking"} |
               {"type":"audio","seq":n}  (immediately followed by one binary MP3 frame) |
               {"type":"interrupted"} | {"type":"done"} | {"type":"error","detail":..}
"""

from __future__ import annotations

import asyncio
import json
import logging

import numpy as np
from fastapi import APIRouter, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from config import SAMPLE_RATE
from app.services.llm import get_llm
from app.services.voice.stt import get_stt
from app.services.voice.tts import get_tts
from app.services.voice.vad import get_vad, FRAME_SAMPLES
from app.services.voice.pipeline import sentence_chunks

logger = logging.getLogger("jarvis.voice")
router = APIRouter(prefix="/voice", tags=["voice"])


# --------------------------------------------------------------------------- #
# POST /voice/stt
# --------------------------------------------------------------------------- #
@router.post("/stt")
async def stt_endpoint(file: UploadFile = File(...)):
    audio = await file.read()
    text = await asyncio.to_thread(get_stt().transcribe, audio)
    return {"text": text}


# --------------------------------------------------------------------------- #
# POST /voice/emotion — Phase 5: read emotion from VOICE TONE (runs in parallel with STT)
# --------------------------------------------------------------------------- #
@router.post("/emotion")
async def emotion_endpoint(file: UploadFile = File(...)):
    """Speech-emotion on the utterance audio. The listener POSTs the same WAV here concurrently
    with the STT call, so it adds ~no latency. Returns {emotion, intensity, scores} or {}."""
    from app.services.emotion.voice import get_voice_emotion
    audio = await file.read()
    res = await asyncio.to_thread(get_voice_emotion().analyze, audio)
    return res or {}


# --------------------------------------------------------------------------- #
# POST /voice/tts/stream
# --------------------------------------------------------------------------- #
class TTSRequest(BaseModel):
    text: str


@router.post("/tts/stream")
async def tts_stream(req: TTSRequest):
    if not req.text.strip():
        return JSONResponse({"detail": "empty text"}, status_code=400)
    mp3 = await get_tts().synthesize_mp3(req.text)

    async def _gen():
        # Chunk the MP3 so the client can begin playback while it downloads.
        for i in range(0, len(mp3), 8192):
            yield mp3[i : i + 8192]

    return StreamingResponse(_gen(), media_type="audio/mpeg")


# --------------------------------------------------------------------------- #
# WS /voice/converse — full-duplex with barge-in
# --------------------------------------------------------------------------- #
# End-of-speech: stop recording after this much trailing silence once speech has started.
_SILENCE_HANG_MS = 700
_MIN_SPEECH_MS = 250          # ignore blips shorter than this
_BARGE_SPEECH_MS = 200        # this much speech during playback => barge-in


class _Converse:
    """Per-connection conversation state machine."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.vad = get_vad()
        self.llm = get_llm()
        self.tts = get_tts()
        self.stt = get_stt()
        self.history: list[dict] = []
        self.speaking = False           # JARVIS currently emitting audio
        self.barge = asyncio.Event()    # set when user interrupts during playback
        self._pcm_residue = np.zeros(0, dtype=np.float32)

    async def run(self) -> None:
        await self.ws.accept()
        await self._send({"type": "listening"})
        utter = bytearray()             # int16 PCM of the current utterance
        speech_ms = 0.0
        silence_ms = 0.0
        in_speech = False
        frame_ms = FRAME_SAMPLES / SAMPLE_RATE * 1000

        while True:
            try:
                msg = await self.ws.receive()
            except (WebSocketDisconnect, RuntimeError):
                return

            if msg.get("type") == "websocket.disconnect":
                return

            # Control text messages
            if msg.get("text") is not None:
                try:
                    ctrl = json.loads(msg["text"])
                except Exception:  # noqa: BLE001
                    ctrl = {}
                if ctrl.get("type") == "reset":
                    self.history.clear()
                continue

            data = msg.get("bytes")
            if not data:
                continue

            pcm16 = np.frombuffer(data, dtype=np.int16)
            pcmf = pcm16.astype(np.float32) / 32768.0

            # If JARVIS is speaking, watch for barge-in instead of accumulating.
            if self.speaking:
                if self._barge_check(pcmf, frame_ms):
                    self.barge.set()
                continue

            # Otherwise accumulate an utterance, tracking speech/silence via VAD.
            for f0 in range(0, len(pcmf) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
                frame = pcmf[f0 : f0 + FRAME_SAMPLES]
                is_sp = self.vad.is_speech(frame)
                if is_sp:
                    in_speech = True
                    speech_ms += frame_ms
                    silence_ms = 0.0
                    utter.extend((frame * 32767).astype(np.int16).tobytes())
                elif in_speech:
                    silence_ms += frame_ms
                    utter.extend((frame * 32767).astype(np.int16).tobytes())
                    if silence_ms >= _SILENCE_HANG_MS:
                        # End of utterance.
                        if speech_ms >= _MIN_SPEECH_MS:
                            await self._handle_utterance(bytes(utter))
                        utter = bytearray()
                        speech_ms = silence_ms = 0.0
                        in_speech = False
                        self.vad.reset()
                        await self._send({"type": "listening"})

    def _barge_check(self, pcmf: np.ndarray, frame_ms: float) -> bool:
        speech = 0.0
        for f0 in range(0, len(pcmf) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
            if self.vad.is_speech(pcmf[f0 : f0 + FRAME_SAMPLES]):
                speech += frame_ms
                if speech >= _BARGE_SPEECH_MS:
                    return True
        return False

    async def _handle_utterance(self, pcm_bytes: bytes) -> None:
        pcm_np = np.frombuffer(pcm_bytes, dtype=np.int16)
        text = await asyncio.to_thread(self.stt.transcribe, pcm_np)
        text = text.strip()
        if not text:
            return
        await self._send({"type": "transcript", "text": text})

        # Stream LLM -> sentences -> TTS, watching for barge-in.
        self.barge.clear()
        self.speaking = True
        reply_parts: list[str] = []
        seq = 0
        try:
            await self._send({"type": "speaking"})
            stream = self.llm.chat_stream(text, history=self.history)
            async for sentence in sentence_chunks(stream):
                if self.barge.is_set():
                    break
                reply_parts.append(sentence)
                await self._send({"type": "reply", "text": sentence})
                mp3 = await self.tts.synthesize_mp3(sentence)
                if self.barge.is_set():
                    break
                await self._send({"type": "audio", "seq": seq})
                await self.ws.send_bytes(mp3)
                seq += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("converse reply failed")
            await self._send({"type": "error", "detail": str(e)})
        finally:
            self.speaking = False

        full = " ".join(reply_parts).strip()
        if self.barge.is_set():
            await self._send({"type": "interrupted"})
        if full:
            self.history.append({"role": "user", "content": text})
            self.history.append({"role": "assistant", "content": full})
            self.history[:] = self.history[-20:]   # cap context
        await self._send({"type": "done"})

    async def _send(self, obj: dict) -> None:
        try:
            await self.ws.send_text(json.dumps(obj))
        except Exception:  # noqa: BLE001
            pass


@router.websocket("/converse")
async def converse(ws: WebSocket):
    await _Converse(ws).run()
