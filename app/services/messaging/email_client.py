"""
Phase 7 — Email (Gmail) over IMAP + SMTP with a free App Password.

We deliberately use IMAP/SMTP rather than the Gmail OAuth API: an App Password
(https://myaccount.google.com/apppasswords) is free, needs no Google Cloud project, no
consent-screen verification, and no credit card — and it gives full read + send. That's the
best *free* option (RULES §4), so it's the one we ship.

  - read   : IMAP, UNSEEN search with BODY.PEEK (reading here never marks mail read in Gmail)
  - send   : SMTP over SSL (smtp.gmail.com:465)
  - reply  : proper In-Reply-To / References threading so it lands in the original thread

All network calls are blocking stdlib (imaplib/smtplib); callers wrap them in
asyncio.to_thread. If creds are absent the client is simply disabled and every method
returns a clean, in-character "not connected" signal instead of raising.
"""

from __future__ import annotations

import email
import imaplib
import logging
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parsedate_to_datetime

from config import (GMAIL_ADDRESS, GMAIL_APP_PASSWORD, GMAIL_IMAP_HOST, GMAIL_SMTP_HOST,
                    GMAIL_AUTOSEND_WHITELIST)

logger = logging.getLogger("jarvis.messaging.email")


class EmailError(RuntimeError):
    pass


@dataclass
class EmailMsg:
    uid: str
    message_id: str
    from_addr: str
    from_name: str
    subject: str
    date_ts: float
    snippet: str
    body: str

    @property
    def display(self) -> str:
        return self.from_name or self.from_addr


def _dec(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:  # noqa: BLE001
        return value.strip()


def _plain_body(msg: email.message.Message) -> str:
    """Best-effort plain-text body extraction."""
    if msg.is_multipart():
        # Prefer text/plain; fall back to stripping a text/html part.
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                    part.get("Content-Disposition", "")):
                return _decode_part(part)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return _strip_html(_decode_part(part))
        return ""
    if msg.get_content_type() == "text/html":
        return _strip_html(_decode_part(msg))
    return _decode_part(msg)


def _decode_part(part: email.message.Message) -> str:
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _strip_html(html: str) -> str:
    import re
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    return re.sub(r"\s+", " ", text).strip()


class EmailClient:
    def __init__(self) -> None:
        self.address = GMAIL_ADDRESS
        self._password = GMAIL_APP_PASSWORD
        self.imap_host = GMAIL_IMAP_HOST
        self.smtp_host = GMAIL_SMTP_HOST

    @property
    def enabled(self) -> bool:
        return bool(self.address and self._password)

    # ---- IMAP read -------------------------------------------------------- #
    def _imap(self) -> imaplib.IMAP4_SSL:
        m = imaplib.IMAP4_SSL(self.imap_host)
        try:
            m.login(self.address, self._password)
        except imaplib.IMAP4.error as e:
            raise EmailError(f"Gmail login failed — check GMAIL_APP_PASSWORD ({e}).") from e
        return m

    def fetch_unread(self, limit: int = 15) -> list[EmailMsg]:
        """Most recent UNSEEN messages. Uses PEEK so reading them here does NOT mark them
        read in Gmail (the boss decides what counts as read)."""
        return self._fetch("UNSEEN", limit)

    def fetch_recent(self, limit: int = 15) -> list[EmailMsg]:
        return self._fetch("ALL", limit)

    def _fetch(self, criteria: str, limit: int) -> list[EmailMsg]:
        if not self.enabled:
            raise EmailError("Email isn't connected.")
        m = self._imap()
        out: list[EmailMsg] = []
        try:
            m.select("INBOX", readonly=True)
            typ, data = m.search(None, criteria)
            if typ != "OK" or not data or not data[0]:
                return []
            uids = data[0].split()[-limit:]
            for uid in reversed(uids):
                typ, msg_data = m.fetch(uid, "(BODY.PEEK[])")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                out.append(self._to_msg(uid.decode(), msg))
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass
        return out

    @staticmethod
    def _to_msg(uid: str, msg: email.message.Message) -> EmailMsg:
        from_raw = _dec(msg.get("From"))
        name, addr = "", from_raw
        parsed = getaddresses([msg.get("From", "")])
        if parsed:
            name, addr = _dec(parsed[0][0]), parsed[0][1]
        subject = _dec(msg.get("Subject")) or "(no subject)"
        try:
            dt = parsedate_to_datetime(msg.get("Date"))
            ts = dt.timestamp() if dt else time.time()
        except Exception:  # noqa: BLE001
            ts = time.time()
        body = _plain_body(msg)
        snippet = " ".join(body.split())[:160]
        return EmailMsg(uid=uid, message_id=(msg.get("Message-ID") or "").strip(),
                        from_addr=addr.lower(), from_name=name, subject=subject,
                        date_ts=ts, snippet=snippet, body=body)

    def find(self, query: str, search_unread_first: bool = True) -> EmailMsg | None:
        """Locate a recent message whose sender name/address or subject matches `query`
        (case-insensitive substring) — used to target a reply by description."""
        q = (query or "").lower().strip()
        if not q:
            return None
        pools = [self.fetch_unread(30), self.fetch_recent(30)] if search_unread_first \
            else [self.fetch_recent(30)]
        for pool in pools:
            for msg in pool:
                hay = f"{msg.from_name} {msg.from_addr} {msg.subject}".lower()
                if q in hay:
                    return msg
        return None

    # ---- SMTP send -------------------------------------------------------- #
    def send(self, to: str, subject: str, body: str, *, in_reply_to: str = "",
             references: str = "") -> str:
        if not self.enabled:
            raise EmailError("Email isn't connected.")
        if not to:
            raise EmailError("No recipient.")
        msg = EmailMessage()
        msg["From"] = formataddr(("", self.address))
        msg["To"] = to
        msg["Subject"] = subject or "(no subject)"
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.address.split("@")[-1])
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = (references + " " + in_reply_to).strip()
        msg.set_content(body or "")
        ctx = ssl.create_default_context()
        try:
            with smtplib.SMTP_SSL(self.smtp_host, 465, context=ctx, timeout=20) as s:
                s.login(self.address, self._password)
                s.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            raise EmailError(f"Gmail rejected the login ({e}).") from e
        except Exception as e:  # noqa: BLE001
            raise EmailError(f"Send failed: {e}") from e
        return msg["Message-ID"]

    def reply(self, original: EmailMsg, body: str) -> str:
        subject = original.subject
        if not subject.lower().startswith("re:"):
            subject = "Re: " + subject
        return self.send(original.from_addr, subject, body,
                         in_reply_to=original.message_id, references=original.message_id)

    @staticmethod
    def is_whitelisted(addr: str) -> bool:
        return (addr or "").lower() in GMAIL_AUTOSEND_WHITELIST


_client: EmailClient | None = None


def get_email() -> EmailClient:
    global _client
    if _client is None:
        _client = EmailClient()
    return _client
