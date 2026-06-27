"""
Face biometrics (Phase 11) — OpenCV YuNet + SFace, free & fully local (NO dlib).

OpenCV (already a dependency) ships two small ONNX models we use directly:
  * **YuNet** — a fast, accurate face DETECTOR (~230 KB).
  * **SFace** — a face RECOGNISER that turns an aligned face into a 128-dim embedding (~37 MB).

Both are downloaded once from the public OpenCV Zoo (free, no key) and then run offline on CPU
in a few milliseconds. This is the modern, Windows-native path — dlib/`face_recognition` need a
C++ toolchain that won't build here, so we deliberately use OpenCV's built-ins instead.

Face is the OPTIONAL SECOND factor: used only when a camera frame is on hand for a sensitive op,
never required (the boss is usually talking from another room). So it costs nothing per turn.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from config import FACE_MODELS_DIR, IDENTITY_FACE_THRESHOLD

logger = logging.getLogger("jarvis.identity.face")

_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
_MODELS = {
    "face_detection_yunet_2023mar.onnx": f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx": f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}

_detector = None
_recognizer = None
_lock = threading.Lock()
DIM = 128


def _ensure_models() -> tuple:
    det_path = FACE_MODELS_DIR / "face_detection_yunet_2023mar.onnx"
    rec_path = FACE_MODELS_DIR / "face_recognition_sface_2021dec.onnx"
    for name, url in _MODELS.items():
        p = FACE_MODELS_DIR / name
        if p.exists() and p.stat().st_size > 1000:
            continue
        import httpx
        logger.info("downloading face model %s (free, OpenCV Zoo) …", name)
        r = httpx.get(url, timeout=180, follow_redirects=True)
        r.raise_for_status()
        p.write_bytes(r.content)
    return det_path, rec_path


def _engines():
    global _detector, _recognizer
    if _detector is None or _recognizer is None:
        with _lock:
            if _detector is None or _recognizer is None:
                import cv2
                det_path, rec_path = _ensure_models()
                _detector = cv2.FaceDetectorYN_create(str(det_path), "", (320, 320), 0.7, 0.3, 5000)
                _recognizer = cv2.FaceRecognizerSF_create(str(rec_path), "")
    return _detector, _recognizer


def available() -> bool:
    try:
        import cv2  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def embed_largest(img_bgr: np.ndarray) -> np.ndarray | None:
    """Detect faces in a BGR image, take the LARGEST, return its 128-d SFace embedding (or None
    if no face is found)."""
    try:
        import cv2
        det, rec = _engines()
        h, w = img_bgr.shape[:2]
        det.setInputSize((w, h))
        _, faces = det.detect(img_bgr)
        if faces is None or len(faces) == 0:
            return None
        # largest by box area (column 2*3 = w*h)
        faces = sorted(faces, key=lambda f: float(f[2]) * float(f[3]), reverse=True)
        aligned = rec.alignCrop(img_bgr, faces[0])
        feat = rec.feature(aligned)
        return np.asarray(feat, dtype=np.float32).flatten()
    except Exception as e:  # noqa: BLE001
        logger.warning("face embed failed: %s", e)
        return None


def embed_from_bytes(img_bytes: bytes) -> np.ndarray | None:
    try:
        import cv2
        arr = np.frombuffer(img_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return embed_largest(img) if img is not None else None
    except Exception as e:  # noqa: BLE001
        logger.warning("face decode failed: %s", e)
        return None


def embed_from_data_url(data_url: str) -> np.ndarray | None:
    """Accepts a 'data:image/jpeg;base64,…' string (what the PWA camera sends) or raw base64."""
    try:
        import base64
        b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
        return embed_from_bytes(base64.b64decode(b64))
    except Exception as e:  # noqa: BLE001
        logger.warning("face data-url decode failed: %s", e)
        return None


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two SFace embeddings."""
    if a is None or b is None:
        return 0.0
    a = np.asarray(a, dtype=np.float32).flatten(); b = np.asarray(b, dtype=np.float32).flatten()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def is_match(a: np.ndarray, b: np.ndarray) -> bool:
    return similarity(a, b) >= IDENTITY_FACE_THRESHOLD


def average(feats: list[np.ndarray]) -> np.ndarray:
    return np.mean(np.stack([np.asarray(f, dtype=np.float32).flatten() for f in feats]), axis=0).astype(np.float32)
