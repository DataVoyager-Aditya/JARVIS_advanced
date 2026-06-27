"""
Encryption at rest for enrolled biometrics (Phase 11).

Biometric vectors (voiceprints, face encodings) are personal data — they must never sit on
disk in the clear. We encrypt them with the **Windows DPAPI** (Data Protection API) via a tiny
`ctypes` shim: `CryptProtectData` / `CryptUnprotectData` from `crypt32.dll`. DPAPI ties the
ciphertext to the current Windows user account (the "OS keystore key" the planner calls for) —
no password to manage, nothing extra to install, and the blob is useless if copied to another
machine or user.

Off Windows (or if DPAPI ever fails), we fall back to Fernet (`cryptography`) with a key kept in
`identity/.fmk` (0600) so the data is still encrypted, just with a local key file. Either way the
public API is the same: `protect(bytes) -> bytes`, `unprotect(bytes) -> bytes`.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("jarvis.identity.crypto")

# A 1-byte scheme tag is prepended so we can always tell how a blob was sealed and unseal it the
# matching way (and migrate later without guessing).
_TAG_DPAPI = b"D"
_TAG_FERNET = b"F"


# --------------------------------------------------------------------------- #
# Windows DPAPI via ctypes (no pywin32 dependency)
# --------------------------------------------------------------------------- #
def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi(call: str, data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def to_blob(b: bytes) -> "DATA_BLOB":
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = to_blob(data)
    blob_out = DATA_BLOB()
    fn = crypt32.CryptProtectData if call == "protect" else crypt32.CryptUnprotectData
    # description=None, entropy=None, reserved=None, prompt=None, flags=CRYPTPROTECT_UI_FORBIDDEN(0x1)
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"DPAPI {call} failed (err {ctypes.GetLastError()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# --------------------------------------------------------------------------- #
# Fernet fallback (local key file)
# --------------------------------------------------------------------------- #
def _fernet(key_path: Path):
    from cryptography.fernet import Fernet
    if not key_path.exists():
        key_path.write_bytes(Fernet.generate_key())
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
    return Fernet(key_path.read_bytes())


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def protect(data: bytes, fallback_key: Path) -> bytes:
    """Encrypt `data`. Prefers DPAPI; falls back to a local Fernet key. Returns tagged ciphertext."""
    if _dpapi_available():
        try:
            return _TAG_DPAPI + _dpapi("protect", data)
        except Exception as e:  # noqa: BLE001
            logger.warning("DPAPI protect failed (%s) — using local key fallback", e)
    return _TAG_FERNET + _fernet(fallback_key).encrypt(data)


def unprotect(blob: bytes, fallback_key: Path) -> bytes:
    """Decrypt a blob produced by protect()."""
    if not blob:
        return b""
    tag, body = blob[:1], blob[1:]
    if tag == _TAG_DPAPI:
        return _dpapi("unprotect", body)
    if tag == _TAG_FERNET:
        return _fernet(fallback_key).decrypt(body)
    raise ValueError("unknown identity ciphertext tag")
