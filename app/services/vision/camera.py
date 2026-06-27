"""
Phase 2 — Vision: webcam capture (OpenCV).

Grabs a single frame from the default webcam for the hands-free "what is this, JARVIS?" flow —
he points a camera and asks. A few frames are discarded first so auto-exposure/white-balance
settle (otherwise the first frame is often black). Capture is blocking → wrap in a thread.
The camera is opened and released per shot; JARVIS never holds the webcam open.
"""

from __future__ import annotations

import logging
import os

# Silence OpenCV's noisy VIDEOIO backend warnings — we deliberately probe DSHOW→MSMF→default and
# expect some to fail per machine; that's normal, not an error worth printing.
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
os.environ.setdefault("OPENCV_VIDEOIO_DEBUG", "0")

from PIL import Image

from config import CAMERA_INDEX
from .multimodal import VisionError

logger = logging.getLogger("jarvis.vision.camera")


def _sharpness(cv2, frame) -> float:
    """Variance of the Laplacian — a standard focus/blur metric (higher = sharper)."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _try_open(cv2, index: int, backend: int, warmup_frames: int):
    """Try one (index, backend); return the BEST PIL frame or None. Requests a real resolution,
    lets auto-exposure/focus settle (webcams — MSMF especially — start black and hunt focus for
    ~1s), then keeps the sharpest well-lit frame among several candidates (kills motion blur and
    the dark/blurry shots that made the model say 'I can't make it out'). Always releases."""
    import time
    cap = None
    try:
        cap = cv2.VideoCapture(index, backend) if backend else cv2.VideoCapture(index)
        if not cap or not cap.isOpened():
            return None
        # Ask for a decent frame size so close-up objects are legible (best-effort; ignored if
        # the device doesn't support it).
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        except Exception:  # noqa: BLE001
            pass

        # Warm-up: drain frames for ~1.2s so exposure/white-balance/focus settle.
        warm_until = time.time() + 1.2
        last = None
        while time.time() < warm_until:
            ok, frame = cap.read()
            if ok and frame is not None:
                last = frame
            time.sleep(0.03)

        # Capture several candidates; keep the sharpest one that isn't basically black.
        best, best_score = None, -1.0
        for _ in range(8):
            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.02)
                continue
            last = frame
            if float(frame.mean()) < 6.0:        # still a black/covered frame
                time.sleep(0.02)
                continue
            score = _sharpness(cv2, frame)
            if score > best_score:
                best, best_score = frame, score
            time.sleep(0.03)

        chosen = best if best is not None else last
        if chosen is None:
            return None
        return Image.fromarray(cv2.cvtColor(chosen, cv2.COLOR_BGR2RGB))
    except Exception:  # noqa: BLE001 — a backend that can't bind raises; just try the next
        return None
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass


_LAST_GOOD: tuple[int, int] | None = None     # (index, backend) that worked last — tried first


def capture_webcam(index: int | None = None, warmup_frames: int = 4) -> Image.Image:
    """Capture one RGB frame from the webcam. Tries the configured index across DSHOW → MSMF →
    default backends (and a couple of fallback indices), since which one works varies by machine.
    The first combo that works is cached so later shots skip the slow failing probes.
    Raises VisionError if no camera can be opened anywhere."""
    global _LAST_GOOD
    try:
        import cv2
    except Exception as e:  # noqa: BLE001
        raise VisionError("No webcam support on this machine (OpenCV not installed).") from e

    idx = CAMERA_INDEX if index is None else index
    backends = [getattr(cv2, "CAP_DSHOW", 0), getattr(cv2, "CAP_MSMF", 0), 0]   # 0 = OpenCV's default
    indices = list(dict.fromkeys([idx, 0, 1, 2]))                              # dedup, keep order
    combos = [(ix, be) for be in dict.fromkeys(backends) for ix in indices]
    if _LAST_GOOD in combos:                  # try the known-good combo first (fast path)
        combos.remove(_LAST_GOOD)
        combos.insert(0, _LAST_GOOD)
    for ix, be in combos:
        img = _try_open(cv2, ix, be, warmup_frames)
        if img is not None:
            _LAST_GOOD = (ix, be)
            return img
    raise VisionError("I can't find a webcam, sir — is one connected, enabled in Windows, "
                      "and not in use by another app (a browser tab, Zoom, the camera app)?")
