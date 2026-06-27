"""
Phase 7 — incoming-message importance classifier (one free LLM call, key-rotated).

When a new message lands (email/WhatsApp/IG DM) the poller asks the LLM to rate how much it
deserves the boss's attention and to write a one-line gist. That rating drives two things:
  1. whether JARVIS proactively announces it (high importance -> spoken/HUD notification),
  2. ranking in the unified inbox.

It degrades gracefully: if the LLM is unreachable or returns junk, we fall back to a cheap
heuristic so the message is still stored and surfaced — never lost.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("jarvis.messaging.classify")

_LEVELS = {"high", "normal", "low"}

_PROMPT = """You triage incoming messages for JARVIS, a personal AI assistant serving one boss.
Given ONE message, judge how much it warrants his attention and summarise it in one short line.

Return ONLY a JSON object, no prose:
{"importance": "high|normal|low", "summary": "<=12 word gist of what they want"}

Guidance:
- high  = a real person needs a reply, time-sensitive, money/security/work-critical, or someone close.
- normal= a genuine message worth seeing but not urgent.
- low   = newsletters, promotions, automated/no-reply, OTP codes, spam, notifications.
Keep the summary plain and specific. No names of the assistant. No markdown."""

# Obvious low-value senders/markers — short-circuit without spending an LLM call.
_LOW_HINTS = re.compile(
    r"\b(no[-_ ]?reply|do[-_ ]?not[-_ ]?reply|newsletter|unsubscribe|promo(tion)?|"
    r"sale|deal|offer|verify your|one[- ]time (code|password)|otp|notification)\b", re.I)


def _heuristic(sender: str, body: str) -> tuple[str, str]:
    text = f"{sender} {body}"
    if _LOW_HINTS.search(text):
        return "low", (body.strip().splitlines()[0][:80] if body.strip() else "(no content)")
    gist = " ".join(body.split())[:80] or "(no content)"
    return "normal", gist


def heuristic_only(sender: str, subject: str, body: str) -> tuple[str, str]:
    """Fast, FREE classification (no LLM call). Used for on-demand inbox refreshes so asking
    'what's in my inbox' never burns a stack of LLM calls / rate budget."""
    text = f"{sender} {subject} {body}"
    if _LOW_HINTS.search(text):
        return "low", (subject or body.strip().splitlines()[0] if body.strip() else "(no content)")[:80]
    gist = " ".join((subject or body).split())[:80] or "(no content)"
    return "normal", gist


async def classify(sender: str, body: str, *, subject: str = "", channel: str = "") -> tuple[str, str]:
    """Return (importance, one_line_summary). Never raises."""
    body = (body or "").strip()
    sender = (sender or "").strip()
    if not body and not subject:
        return "low", "(empty)"
    # Cheap pre-filter for the obvious automated stuff (saves the call + rate budget).
    if _LOW_HINTS.search(f"{sender} {subject} {body}"):
        return "low", (subject or body.splitlines()[0])[:80]

    payload = f"channel: {channel or 'message'}\nfrom: {sender}"
    if subject:
        payload += f"\nsubject: {subject}"
    payload += f"\nbody: {body[:1500]}"

    try:
        from app.services.llm.key_rotator import get_rotator
        raw = await get_rotator().chat(
            [{"role": "system", "content": _PROMPT}, {"role": "user", "content": payload}],
            task="chat", temperature=0.1)
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            imp = str(data.get("importance", "")).lower().strip()
            summ = " ".join(str(data.get("summary", "")).split())[:120]
            if imp in _LEVELS and summ:
                return imp, summ
    except Exception as e:  # noqa: BLE001
        logger.debug("classify fell back to heuristic: %s", e)
    return _heuristic(sender, subject or body)
