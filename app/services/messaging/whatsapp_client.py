"""
Phase 7 — WhatsApp bridge (the Python side).

WhatsApp has no free official API, so we drive a local Node sidecar (sidecars/whatsapp/)
running whatsapp-web.js — the same engine WhatsApp Web uses. It pairs once by QR (session
persists on disk) and exposes a tiny local HTTP API. This module is the thin async client
the rest of JARVIS talks to; new incoming messages are *pushed* by the sidecar to our
webhook (routers/messaging.py), so we don't poll.

Everything is best-effort: if the sidecar isn't running, methods raise WhatsAppError with a
clear message and the tools relay it in character — never a crash.
"""

from __future__ import annotations

import logging

import re

import httpx

from config import WHATSAPP_SIDECAR_URL, WHATSAPP_ENABLED, WHATSAPP_WEBHOOK_TOKEN

logger = logging.getLogger("jarvis.messaging.whatsapp")

_PHONE_ONLY = re.compile(r"^[\d\s+()\-]+$")


def friendly_name(name: str) -> str:
    """A saved-contact name as-is, but a bare phone number (no saved name) becomes 'an unknown
    number' — JARVIS shouldn't read out raw digits unless asked. The real number stays in the
    chat_id, so it's still retrievable on request."""
    name = (name or "").strip()
    if not name or _PHONE_ONLY.match(name):
        return "an unknown number"
    return name


class WhatsAppError(RuntimeError):
    pass


def _sidecar_error(e: "httpx.HTTPStatusError") -> str:
    """Turn the sidecar's error response into a clear, human message."""
    raw = ""
    try:
        raw = (e.response.json() or {}).get("error", "")
    except Exception:  # noqa: BLE001
        raw = ""
    low = raw.lower()
    if "no contact" in low or "no number" in low:
        # raw is like: no contact matching "Carl"
        return (f"I couldn't find that contact on WhatsApp ({raw}). The saved name may differ — "
                f"add it to MY_CONTACTS.txt or tell me their exact saved name / number.")
    if "not ready" in low or e.response.status_code == 409:
        return "WhatsApp is still connecting — give it a few seconds and try again."
    return raw or f"WhatsApp request failed ({e.response.status_code})."


class WhatsAppClient:
    def __init__(self) -> None:
        self.base = WHATSAPP_SIDECAR_URL
        self._headers = {"X-JARVIS-Token": WHATSAPP_WEBHOOK_TOKEN}

    @property
    def enabled(self) -> bool:
        return WHATSAPP_ENABLED

    async def _get(self, path: str, timeout: float = 50, **kw) -> dict:
        if not self.enabled:
            raise WhatsAppError("WhatsApp is turned off.")
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(self.base + path, headers=self._headers, **kw)
                r.raise_for_status()
                return r.json()
        except httpx.HTTPStatusError as e:
            raise WhatsAppError(_sidecar_error(e)) from e
        except httpx.HTTPError as e:
            raise WhatsAppError(
                "WhatsApp sidecar isn't reachable — start it with `node sidecars/whatsapp` "
                f"and scan the QR. ({type(e).__name__})") from e

    async def _post(self, path: str, payload: dict) -> dict:
        if not self.enabled:
            raise WhatsAppError("WhatsApp is turned off.")
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(self.base + path, headers=self._headers, json=payload)
                r.raise_for_status()
                return r.json()
        except httpx.HTTPStatusError as e:
            raise WhatsAppError(_sidecar_error(e)) from e
        except httpx.HTTPError as e:
            raise WhatsAppError(
                "WhatsApp sidecar isn't reachable — start it with `node sidecars/whatsapp`. "
                f"({type(e).__name__})") from e

    async def status(self) -> dict:
        """{ready: bool, state: 'ready'|'qr'|'loading'|..., qr: <ascii?>, me: <number?>}."""
        try:
            return await self._get("/status", timeout=8)
        except WhatsAppError as e:
            return {"ready": False, "state": "offline", "reason": str(e)}

    async def inbox(self, limit: int = 15) -> list[dict]:
        """Recent unread/recent chats: [{chat_id, name, body, ts, unread}]."""
        data = await self._get("/inbox", params={"limit": limit})
        return data.get("messages", [])

    async def send(self, to: str, message: str) -> dict:
        """`to` may be a saved-contact name, a chat_id (…@c.us), or a phone number."""
        return await self._post("/send", {"to": to, "message": message})

    # ---- synchronous variants (for the agent tools, which run sync on the loop) ---- #
    def _req_sync(self, method: str, path: str, timeout: float = 20, **kw) -> dict:
        if not self.enabled:
            raise WhatsAppError("WhatsApp is turned off.")
        try:
            with httpx.Client(timeout=timeout) as c:
                r = c.request(method, self.base + path, headers=self._headers, **kw)
                r.raise_for_status()
                return r.json()
        except httpx.ReadTimeout as e:
            # The sidecar IS up but a (cold) read took too long — report that accurately rather
            # than claiming WhatsApp is disconnected.
            raise WhatsAppError(
                "WhatsApp is connected but still loading your chats — give it a few seconds and "
                "try again.") from e
        except httpx.HTTPStatusError as e:
            # The sidecar is UP but returned an error (e.g. "no contact matching X", "not
            # ready") — surface ITS message instead of wrongly saying it's disconnected.
            raise WhatsAppError(_sidecar_error(e)) from e
        except httpx.HTTPError as e:
            raise WhatsAppError(
                "WhatsApp sidecar isn't reachable — start it with `node sidecars/whatsapp` "
                f"and scan the QR. ({type(e).__name__})") from e

    def status_sync(self) -> dict:
        try:
            return self._req_sync("GET", "/status", timeout=8)
        except WhatsAppError as e:
            return {"ready": False, "state": "offline", "reason": str(e)}

    # Chat reads can be slow on the FIRST call for a big account (cold getChats); the sidecar
    # warms the cache on connect, but allow generous time just in case.
    def inbox_sync(self, limit: int = 15) -> list[dict]:
        return self._req_sync("GET", "/inbox", timeout=50, params={"limit": limit}).get("messages", [])

    def send_sync(self, to: str, message: str) -> dict:
        return self._req_sync("POST", "/send", timeout=40, json={"to": to, "message": message})

    def chat_sync(self, who: str, limit: int = 15) -> dict:
        return self._req_sync("GET", "/chat", timeout=50, params={"id": who, "limit": limit})

    def mark_read_sync(self, to: str) -> dict:
        return self._req_sync("POST", "/read", timeout=30, json={"to": to})

    def delete_sync(self, to: str, everyone: bool = True) -> dict:
        return self._req_sync("POST", "/delete", timeout=40,
                              json={"to": to, "everyone": bool(everyone)})


_client: WhatsAppClient | None = None


def get_whatsapp() -> WhatsAppClient:
    global _client
    if _client is None:
        _client = WhatsAppClient()
    return _client
