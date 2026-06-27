"""
Phase 2 — Vision: image encoding + the multimodal LLM call.

Frames (screen / webcam / uploaded image) are downscaled and JPEG-encoded to a small data URL,
then sent to a free vision model via the KeyRotator's `vision` task. Routing (providers.py):
Groq Llama-4 Scout is primary (fast LPU); Gemini 2.5 Flash is the accuracy fallback (better at
dense text / OCR). A caller can pass prefer="gemini" to put accuracy first for screen-reading
while still falling back to Groq if Gemini is congested.
"""

from __future__ import annotations

import base64
import io
import logging

from PIL import Image, ImageOps, ImageEnhance

from config import VISION_MAX_WIDTH, VISION_JPEG_QUALITY

logger = logging.getLogger("jarvis.vision")


def enhance_photo(img: Image.Image) -> Image.Image:
    """Clean up a webcam frame so the model can actually read it — real webcams in normal rooms
    come out dim, low-contrast and soft. Auto-contrast fixes the washed-out exposure, a brightness
    nudge lifts dark frames, and a sharpness/colour bump recovers detail. (Screen captures are
    already crisp, so this is only applied to camera frames.)"""
    try:
        img = img.convert("RGB")
        img = ImageOps.autocontrast(img, cutoff=1)               # stretch to full range
        if _mean_brightness(img) < 110:                          # only lift genuinely dark frames
            img = ImageEnhance.Brightness(img).enhance(1.25)
        img = ImageEnhance.Sharpness(img).enhance(1.7)
        img = ImageEnhance.Color(img).enhance(1.12)
        return img
    except Exception:  # noqa: BLE001
        return img


def _mean_brightness(img: Image.Image) -> float:
    g = img.convert("L")
    return sum(g.getdata()) / max(1, g.width * g.height)


class VisionError(RuntimeError):
    pass


def to_jpeg_bytes(img: Image.Image) -> bytes:
    """Downscale (aspect-preserved) + JPEG-encode — keeps the payload small/fast and under
    provider request caps, while staying readable for OCR."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    if w > VISION_MAX_WIDTH:
        img = img.resize((VISION_MAX_WIDTH, max(1, int(h * VISION_MAX_WIDTH / w))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=VISION_JPEG_QUALITY)
    return buf.getvalue()


def bytes_to_data_url(raw: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def image_to_data_url(img: Image.Image) -> str:
    return bytes_to_data_url(to_jpeg_bytes(img), "image/jpeg")


def thumb_data_url(img: Image.Image, width: int = 440) -> str:
    """A small JPEG data URL for the HUD viewport (light enough for the event bus)."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    if w > width:
        img = img.resize((width, max(1, int(h * width / w))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=60)
    return bytes_to_data_url(buf.getvalue(), "image/jpeg")


def load_image_bytes(raw: bytes) -> Image.Image:
    """Decode arbitrary uploaded image bytes (png/jpg/webp/…) into a PIL image."""
    try:
        return Image.open(io.BytesIO(raw))
    except Exception as e:  # noqa: BLE001
        raise VisionError(f"that doesn't look like a readable image ({e})") from e


async def describe_data_url(data_url: str, prompt: str, prefer: str = "") -> str:
    """Run the vision LLM on an already-encoded data URL. Returns the model's text."""
    from app.services.llm.key_rotator import get_rotator
    try:
        out = (await get_rotator().vision(prompt, data_url, prefer=prefer)).strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("vision LLM failed: %s", e)
        raise VisionError("My vision models are all busy just now, sir — try again in a moment.") from e
    return out or "I can see the image, but I'm not sure how to describe it, sir."
