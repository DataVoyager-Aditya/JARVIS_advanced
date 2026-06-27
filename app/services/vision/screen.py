"""
Phase 2 — Vision: PC screen capture (cross-platform via mss).

Grabs the primary monitor as a PIL image for "what's on my screen?", reading an on-screen error,
OCR-style questions, or debugging help. Capture is blocking, so callers wrap it in a thread.
"""

from __future__ import annotations

import logging

from PIL import Image

from .multimodal import VisionError

logger = logging.getLogger("jarvis.vision.screen")


def capture_screen(monitor: int = 1) -> Image.Image:
    """Capture one monitor (1 = primary; mss.monitors[0] is the whole virtual desktop)."""
    try:
        import mss
    except Exception as e:  # noqa: BLE001
        raise VisionError("Screen capture isn't available (mss not installed).") from e
    try:
        with mss.mss() as sct:
            mons = sct.monitors
            mon = mons[monitor] if 0 <= monitor < len(mons) else mons[1] if len(mons) > 1 else mons[0]
            shot = sct.grab(mon)
            return Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)
    except Exception as e:  # noqa: BLE001
        raise VisionError(f"Couldn't capture the screen: {e}") from e
