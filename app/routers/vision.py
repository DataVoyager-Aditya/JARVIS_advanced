"""
Phase 2 — Vision endpoints.

  POST /vision/screen    {question?}                  -> server-side screenshot, described
  POST /vision/describe  {image: <data-url|b64>, question?}  -> describe an uploaded frame
                                                              (the PWA camera viewport posts here)

Both return {ok, text} or {ok:false, error}. Never raise to the client.
"""

from __future__ import annotations

import base64
import logging

import os

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.services.vision import get_vision, VisionError
from app.services.vision import LAST_FRAME_PATH

logger = logging.getLogger("jarvis.vision.router")
router = APIRouter(prefix="/vision", tags=["vision"])


class ScreenReq(BaseModel):
    question: str = ""


class DescribeReq(BaseModel):
    image: str = ""          # data URL ("data:image/jpeg;base64,...") or bare base64
    question: str = ""
    ocr: bool = False        # true = prefer the accuracy model (text-heavy frame)


def _decode_image(s: str) -> bytes:
    s = (s or "").strip()
    if not s:
        raise VisionError("No image was sent, sir.")
    if s.startswith("data:"):
        s = s.split(",", 1)[1] if "," in s else ""
    try:
        return base64.b64decode(s)
    except Exception as e:  # noqa: BLE001
        raise VisionError("That image didn't decode, sir.") from e


@router.get("/last")
async def vision_last():
    """Serve the exact frame JARVIS last looked at — open in a browser to inspect capture quality."""
    if not os.path.isfile(LAST_FRAME_PATH):
        return JSONResponse({"ok": False, "error": "Nothing captured yet — ask JARVIS to look at something first."},
                            status_code=404)
    return FileResponse(LAST_FRAME_PATH, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


@router.post("/screen")
async def vision_screen(req: ScreenReq) -> dict:
    try:
        return {"ok": True, "text": await get_vision().describe_screen(req.question)}
    except VisionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        logger.exception("vision/screen failed")
        return {"ok": False, "error": f"Vision failed: {e}"}


@router.post("/describe")
async def vision_describe(req: DescribeReq) -> dict:
    try:
        raw = _decode_image(req.image)
        text = await get_vision().describe_image_bytes(raw, req.question, ocr=req.ocr)
        return {"ok": True, "text": text}
    except VisionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        logger.exception("vision/describe failed")
        return {"ok": False, "error": f"Vision failed: {e}"}
