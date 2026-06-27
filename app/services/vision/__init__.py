"""
Phase 2 — JARVIS's eyes.

Three free, on-demand vision capabilities, all routed through the existing KeyRotator vision task
(Groq Llama-4 primary for speed, Gemini 2.5 Flash fallback for accuracy/OCR):

  - look()            -> grab a webcam frame and answer "what is this?"   (object/scene)
  - describe_screen() -> grab the PC screen and answer "what's on my screen / read this"  (OCR)
  - describe_image()  -> describe an uploaded image (PWA camera) or a local image file

`VisionService` is the single facade. Capture (mss / OpenCV) is blocking, so the async methods
run it in a thread; the LLM call is already async. Everything degrades to a clear in-character
sentence (never a crash) if a camera/screen/model isn't available.
"""

from __future__ import annotations

import asyncio
import logging
import os

from . import multimodal, screen, camera
from .multimodal import VisionError

logger = logging.getLogger("jarvis.vision")

# Where to push the captured frame so the PWA's VISION panel can show what JARVIS just saw.
_EVENTS_URL = os.getenv("JARVIS_EVENTS_URL", "http://127.0.0.1:8000/events/publish")

# Every captured frame (exactly what the model is sent) is written here so the boss can SEE what
# JARVIS saw — open it directly, or GET /vision/last. This is the diagnostic for "is it the photo
# quality or the model?".
from config import BASE_DIR  # noqa: E402
LAST_FRAME_PATH = str(BASE_DIR / "database" / "vision_last.jpg")


def _save_last(jpeg_bytes: bytes) -> None:
    try:
        os.makedirs(os.path.dirname(LAST_FRAME_PATH), exist_ok=True)
        with open(LAST_FRAME_PATH, "wb") as f:
            f.write(jpeg_bytes)
    except Exception:  # noqa: BLE001
        pass


async def _remember_and_publish(img):
    """Save the exact frame the model will see (for inspection) and mirror a thumbnail to the HUD.
    Returns the model-ready data URL so the caller doesn't re-encode. Never raises."""
    raw = await asyncio.to_thread(multimodal.to_jpeg_bytes, img)
    await asyncio.to_thread(_save_last, raw)
    try:
        import httpx
        thumb = await asyncio.to_thread(multimodal.thumb_data_url, img)
        async with httpx.AsyncClient(timeout=2.0) as c:
            await c.post(_EVENTS_URL, json={"type": "vision_frame", "image": thumb})
    except Exception:  # noqa: BLE001
        pass
    return multimodal.bytes_to_data_url(raw, "image/jpeg")

# Screen reading is text/OCR-heavy → prefer the more accurate model (Gemini), still falling back
# to fast Groq if it's congested. Object/scene questions stay on Groq (fast, plenty accurate).
_OCR_PREFER = "gemini"

_DEFAULT_SCREEN_PROMPT = (
    "You are JARVIS looking at your boss's PC screen. Briefly and accurately describe what's on "
    "it — the app/window in focus and the key text or content. If he asked something specific, "
    "answer that. Read any important text verbatim. Be concise and conversational."
)
_DEFAULT_LOOK_PROMPT = (
    "You are JARVIS looking through your boss's camera. Identify what he's showing you and "
    "describe it concisely and accurately. If he asked a specific question about it, answer that."
)
_DEFAULT_IMAGE_PROMPT = (
    "You are JARVIS. Describe this image concisely and accurately for your boss; read any "
    "important text verbatim. If a specific question was asked, answer it directly."
)


class VisionService:
    # ---- webcam: "what is this?" ----------------------------------------- #
    async def look(self, question: str = "") -> str:
        prompt = f"{_DEFAULT_LOOK_PROMPT}\n\nHis question: {question}" if question else _DEFAULT_LOOK_PROMPT
        img = await asyncio.to_thread(camera.capture_webcam)
        img = await asyncio.to_thread(multimodal.enhance_photo, img)   # fix dim/soft webcam frames
        data_url = await _remember_and_publish(img)     # save for inspection + show on the HUD
        return await multimodal.describe_data_url(data_url, prompt)

    # ---- screen: "what's on my screen / read this" ----------------------- #
    async def describe_screen(self, question: str = "") -> str:
        prompt = f"{_DEFAULT_SCREEN_PROMPT}\n\nHis question: {question}" if question else _DEFAULT_SCREEN_PROMPT
        img = await asyncio.to_thread(screen.capture_screen)
        data_url = await _remember_and_publish(img)     # mirror onto the HUD + save
        return await multimodal.describe_data_url(data_url, prompt, prefer=_OCR_PREFER)

    # ---- arbitrary image: uploaded bytes (PWA camera) or a local file ---- #
    async def describe_image_bytes(self, raw: bytes, question: str = "", *, ocr: bool = False) -> str:
        prompt = f"{_DEFAULT_IMAGE_PROMPT}\n\nHis question: {question}" if question else _DEFAULT_IMAGE_PROMPT
        img = await asyncio.to_thread(multimodal.load_image_bytes, raw)
        data_url = await _remember_and_publish(img)     # save for inspection + HUD
        return await multimodal.describe_data_url(data_url, prompt, prefer=_OCR_PREFER if ocr else "")

    async def describe_image_file(self, path: str, question: str = "") -> str:
        path = os.path.expanduser((path or "").strip().strip('"'))
        if not os.path.isfile(path):
            raise VisionError(f"I can't find an image at '{path}', sir.")
        with open(path, "rb") as f:
            raw = f.read()
        return await self.describe_image_bytes(raw, question)


_vision: VisionService | None = None


def get_vision() -> VisionService:
    global _vision
    if _vision is None:
        _vision = VisionService()
    return _vision


__all__ = ["VisionService", "get_vision", "VisionError"]
