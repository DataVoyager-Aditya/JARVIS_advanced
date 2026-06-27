"""
Identity endpoints (Phase 11) — enrolment, verification and roster, for the enrol CLI and the
app's enrolment screen.

Mutating endpoints (enrol / remove / passphrase) are protected by a shared token, so an exposed
tunnel can never be used to enrol a stranger as Owner. `/identity/status` is open & read-only
(it leaks nothing — just whether the Owner is set up).

  GET  /identity/status                       {enabled, enrolled, count, has_passphrase}
  POST /identity/verify   (wav)               who is this voice? (token)
  POST /identity/enroll   (name,tier,wavs…)   enrol/replace a person (token)
  POST /identity/remove   {name}              revoke (token)
  POST /identity/passphrase {passphrase}      set/clear the passphrase (token)
  GET  /identity/roster                       enrolled people + tiers (token)
"""

from __future__ import annotations

import io
import logging

import numpy as np
from fastapi import APIRouter, Header, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from config import IDENTITY_TOKEN, IDENTITY_ENABLED
from app.services import identity as ident

logger = logging.getLogger("jarvis.identity.api")
router = APIRouter(prefix="/identity", tags=["identity"])


def _auth(token: str | None) -> None:
    if (token or "") != IDENTITY_TOKEN:
        raise HTTPException(status_code=401, detail="bad identity token")


def _auth_enroll(token: str | None, session: str) -> None:
    """Enrolment is allowed with the master token (PC/CLI) OR from a session whose voice the
    server has verified as the Owner (the phone, after the Owner spoke). This lets the phone
    enrol people without the master token ever being shipped to the browser."""
    if (token or "") == IDENTITY_TOKEN:
        return
    st = ident.get_session_trust(session or "")
    if st is not None and st.is_owner:
        return
    raise HTTPException(status_code=401, detail="enrolment needs the owner or the token")


def _wav_to_samples(raw: bytes) -> tuple[np.ndarray, int]:
    import soundfile as sf
    data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
    return np.asarray(data, dtype=np.float32), int(sr)


@router.get("/status")
async def status():
    return {"enabled": IDENTITY_ENABLED, "enrolled": ident.is_enrolled(),
            "count": ident.get_store().count(), "has_passphrase": ident.get_store().has_passphrase()}


@router.get("/roster")
async def roster(x_jarvis_token: str | None = Header(default=None)):
    _auth(x_jarvis_token)
    return {"roster": ident.roster()}


@router.post("/verify")
async def verify(file: UploadFile = File(...), x_jarvis_token: str | None = Header(default=None)):
    _auth(x_jarvis_token)
    samples, sr = _wav_to_samples(await file.read())
    t = ident.identify_voice(samples, sr)
    return {"tier": t.tier, "name": t.name, "display": t.display, "confidence": round(t.confidence, 3)}


@router.post("/whoami")
async def whoami(file: UploadFile = File(...), session: str = Form("")):
    """OPEN endpoint for remote surfaces (the phone): identify the speaker from a mic clip and
    remember the SERVER-verified tier for this PWA session, so /chat gates by who's actually
    talking (not a forgeable client claim). Read-only — it can't enrol anyone, so it's safe open."""
    samples, sr = _wav_to_samples(await file.read())
    t = ident.identify_voice(samples, sr)
    if session and t.tier in ("owner", "trusted", "guest", "stranger"):
        ident.set_session_trust(session, t)
    if t.tier in ("owner", "trusted", "guest"):    # a positive match also updates the name panel
        ident.set_active(t)
        await _broadcast_identity()
    return {"tier": t.tier, "name": t.name, "display": t.display, "confidence": round(t.confidence, 3)}


@router.post("/enroll")
async def enroll(name: str = Form(...), tier: str = Form("trusted"),
                 display: str = Form(""), passphrase: str = Form(""),
                 files: list[UploadFile] = File(default=[]),
                 faces: list[UploadFile] = File(default=[]),
                 x_jarvis_token: str | None = Header(default=None)):
    _auth(x_jarvis_token)
    clips = []
    sr = 16000
    for f in files:
        try:
            s, sr = _wav_to_samples(await f.read())
            clips.append(s)
        except Exception as e:  # noqa: BLE001
            logger.warning("enroll: bad wav %s", e)
    face_imgs = []
    for f in faces:
        try:
            import cv2
            arr = np.frombuffer(await f.read(), dtype=np.uint8)
            im = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if im is not None:
                face_imgs.append(im)
        except Exception as e:  # noqa: BLE001
            logger.warning("enroll: bad face image %s", e)
    res = ident.enroll(name=name, tier=tier, clips=clips, sr=sr,
                       face_imgs=face_imgs or None, display=display or name)
    if res.get("ok") and passphrase:
        ident.set_passphrase(passphrase)
    return res


class NameReq(BaseModel):
    name: str


class PhraseReq(BaseModel):
    passphrase: str = ""


@router.post("/remove")
async def remove(req: NameReq, x_jarvis_token: str | None = Header(default=None)):
    _auth(x_jarvis_token)
    return ident.remove(req.name)


@router.post("/passphrase")
async def passphrase(req: PhraseReq, x_jarvis_token: str | None = Header(default=None)):
    _auth(x_jarvis_token)
    return ident.set_passphrase(req.passphrase)


# --------------------------------------------------------------------------- #
# Active session identity (drives the HUD / mobile name panel) + face scan
# --------------------------------------------------------------------------- #
async def _broadcast_identity() -> None:
    try:
        from app.routers.events import broadcast
        await broadcast({"type": "identity", **ident.active_view()})
    except Exception as e:  # noqa: BLE001
        logger.debug("identity broadcast failed: %s", e)


def _grab_frame_bgr():
    """One webcam frame as a BGR numpy array (what the face engine wants), or None."""
    try:
        from app.services.vision.camera import capture_webcam
        pil = capture_webcam()
        return np.array(pil)[:, :, ::-1].copy()        # PIL RGB -> OpenCV BGR
    except Exception as e:  # noqa: BLE001
        logger.warning("webcam grab failed: %s", e)
        return None


@router.get("/active")
async def active():
    """Who is currently using JARVIS — the HUD/mobile poll this on load, then live-update on the
    'identity' event."""
    return ident.active_view()


@router.post("/scan")
async def scan(x_jarvis_token: str | None = Header(default=None)):
    """Grab a webcam frame, recognise the face, set the active user and push it to the UIs. This
    is the startup gate AND the 're-run face recognition' command."""
    _auth(x_jarvis_token)
    frame = _grab_frame_bgr()
    if frame is None:
        return {"ok": False, "tier": "unsure", "reason": "no_camera",
                "message": "I couldn't reach the camera, sir."}
    t = ident.identify_face(frame)
    if t.tier in ("owner", "trusted", "guest"):
        ident.set_active(t)
        await _broadcast_identity()
    return {"ok": True, "tier": t.tier, "name": t.name, "display": t.display,
            "confidence": round(t.confidence, 3), "active": ident.active_view()}


# --------------------------------------------------------------------------- #
# Guided enrolment (the listener drives this after the Owner says "add <name> as <tier>")
# --------------------------------------------------------------------------- #
@router.get("/enroll/pending")
async def enroll_pending(session: str = "", x_jarvis_token: str | None = Header(default=None)):
    _auth_enroll(x_jarvis_token, session)
    return ident.pending_enrollment() or {"pending": False}


@router.post("/enroll/voice")
async def enroll_voice(file: UploadFile = File(...), session: str = "",
                       x_jarvis_token: str | None = Header(default=None)):
    _auth_enroll(x_jarvis_token, session)
    samples, sr = _wav_to_samples(await file.read())
    return ident.add_pending_voice(samples, sr)


@router.post("/enroll/face")
async def enroll_face(file: UploadFile | None = File(default=None), session: str = "",
                      x_jarvis_token: str | None = Header(default=None)):
    """Capture the enrolee's face — from an uploaded image (phone selfie), else the PC webcam."""
    _auth_enroll(x_jarvis_token, session)
    if file is not None:
        import cv2
        arr = np.frombuffer(await file.read(), dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    else:
        img = _grab_frame_bgr()
    if img is None:
        return {"ok": False, "message": "I couldn't get a camera frame."}
    return ident.add_pending_face(img)


@router.post("/enroll/finalize")
async def enroll_finalize(session: str = "", x_jarvis_token: str | None = Header(default=None)):
    _auth_enroll(x_jarvis_token, session)
    res = ident.finalize_enrollment()
    if res.get("ok"):
        await _broadcast_identity()
    return res


@router.post("/enroll/cancel")
async def enroll_cancel(session: str = "", x_jarvis_token: str | None = Header(default=None)):
    _auth_enroll(x_jarvis_token, session)
    return ident.cancel_enrollment()
