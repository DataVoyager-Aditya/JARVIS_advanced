"""
KeyRotator — the generalized 'Groq trick'.

Quota-aware, task-aware rotation across every free provider:
  - route(task) walks active providers by preference, and within each, the least-used key
    with quota left, skipping keys that are rate-limited (circuit-breaker) or out of daily
    quota.
  - 429 / 5xx / network error  -> trip a short breaker on that key, fall to the next key;
    when a whole provider is exhausted, fall to the next provider.
  - Per-key, per-day counts persist in SQLite (database/keys.db) so quota survives restarts.

Public surface used by the rest of JARVIS:
  - async chat_stream(messages, task='chat')  -> yields text deltas
  - async chat(messages, task='chat')         -> full string
  - async vision(text, image_data_url)        -> full string  (task='vision')
  -       transcribe(wav_bytes)               -> text         (task='stt', sync)
  - async embed(texts)                        -> list[list[float]]  (task='embed')
  -       stats()                             -> dict for /admin/key-stats
"""

from __future__ import annotations

import asyncio
import collections
import datetime as _dt
import hashlib
import json
import logging
import sqlite3
import threading
import time
from typing import AsyncIterator

import httpx

from config import DATABASE_DIR
from app.services.llm.providers import (
    active_chat_providers, active_stt_providers, all_specs, ProviderSpec,
)

logger = logging.getLogger("jarvis.rotator")

_DB_PATH = DATABASE_DIR / "keys.db"
_BREAKER_429 = 60.0      # seconds a key rests after a rate-limit
_BREAKER_5XX = 15.0      # seconds a key rests after a server/network error
_HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class ToolUseFailed(Exception):
    """A provider rejected a tool call as malformed (carries the raw error blob)."""


def _today() -> str:
    return _dt.date.today().isoformat()


def _breaker_secs(resp: "httpx.Response") -> float:
    """How long to bench a key after a 429. Daily-quota exhaustion -> long rest; ordinary
    per-minute rate limits -> the server's Retry-After (short), so it recovers quickly."""
    body = resp.text.lower()
    if "quota" in body or "limit: 0" in body or "exceeded your current quota" in body:
        return 1800.0                       # quota/day exhausted — rest it a good while
    # Bench a rate-limited key at least ~12s so it isn't re-poked every turn (which causes
    # the 429 churn). Per-minute limits realistically need that long to clear anyway.
    ra = resp.headers.get("retry-after")
    if ra:
        try:
            return min(max(float(ra), 12.0), 60.0)
        except ValueError:
            pass
    import re
    m = re.search(r"retry_after_seconds[\"\s:]+([0-9.]+)", resp.text)
    if m:
        try:
            return min(max(float(m.group(1)), 12.0), 60.0)
        except ValueError:
            pass
    return 15.0                              # unknown rate limit


def _key_id(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:10]


class KeyRotator:
    def __init__(self) -> None:
        self.chat_providers = active_chat_providers()
        self.stt_providers = active_stt_providers()
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS usage ("
            " provider TEXT, key_id TEXT, day TEXT, count INTEGER,"
            " PRIMARY KEY (provider, key_id, day))"
        )
        self._db.commit()
        # transient state (not persisted)
        self._breaker: dict[tuple[str, str], float] = {}          # (provider,key_id) -> until
        self._rpm: dict[tuple[str, str], collections.deque] = collections.defaultdict(collections.deque)
        # Providers whose free-tier size cap can't fit a full tool-call payload (learned on the first
        # 413). Skipped up-front on later tool calls so we don't re-413 Groq every single action turn
        # (which added a wasted round-trip + log spam and handed every action to the fallback). The
        # no-tools chit-chat path still uses them — it's small enough to fit.
        self._tools_too_big: set[str] = set()

        active = [f"{p.name}({len(p.keys)})" for p in self.chat_providers]
        logger.info("KeyRotator ready — chat providers: %s ; stt: %s",
                    ", ".join(active) or "none",
                    ", ".join(f"{p.name}({len(p.keys)})" for p in self.stt_providers) or "none")

    # ----- quota bookkeeping (SQLite) ----- #
    def _count(self, provider: str, kid: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT count FROM usage WHERE provider=? AND key_id=? AND day=?",
                (provider, kid, _today()),
            ).fetchone()
        return row[0] if row else 0

    def _bump(self, provider: str, kid: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO usage(provider,key_id,day,count) VALUES(?,?,?,1) "
                "ON CONFLICT(provider,key_id,day) DO UPDATE SET count=count+1",
                (provider, kid, _today()),
            )
            self._db.commit()
        self._rpm[(provider, kid)].append(time.monotonic())

    def _rpm_ok(self, provider: str, kid: str, rpm: int) -> bool:
        dq = self._rpm[(provider, kid)]
        now = time.monotonic()
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        return len(dq) < rpm

    def _blocked(self, provider: str, kid: str) -> bool:
        until = self._breaker.get((provider, kid))
        return until is not None and time.monotonic() < until

    def _trip(self, provider: str, kid: str, seconds: float) -> None:
        self._breaker[(provider, kid)] = time.monotonic() + seconds

    # ----- candidate selection ----- #
    def _candidates(self, task: str, providers: list[ProviderSpec], prefer: str = ""):
        """Yield (spec, key_index, key, key_id) best-first for a task. `prefer` (a provider name)
        is tried first when healthy, then normal priority order — used to route OCR/screen-text
        vision to the more accurate model while still falling back to the fast one."""
        pool = []
        for spec in providers:
            if task not in spec.tasks:
                continue
            for i, key in enumerate(spec.keys):
                kid = _key_id(key)
                if self._blocked(spec.name, kid):
                    continue
                if self._count(spec.name, kid) >= spec.daily_limit:
                    continue
                if not self._rpm_ok(spec.name, kid, spec.rpm):
                    continue
                pool.append((spec.priority, self._count(spec.name, kid), spec, i, key, kid))
        # preferred provider first, then by priority, then least-used key
        pool.sort(key=lambda t: (0 if t[2].name == prefer else 1, t[0], t[1]))
        for _, _, spec, i, key, kid in pool:
            yield spec, i, key, kid

    # ------------------------------------------------------------------ #
    # Chat — streaming, OpenAI-compatible SSE
    # ------------------------------------------------------------------ #
    async def chat_stream(
        self, messages: list[dict], task: str = "chat", temperature: float = 0.5,
        prefer: str = "",
    ) -> AsyncIterator[str]:
        last_err: Exception | None = None
        tried = 0
        skip_providers: set[str] = set()       # rejected this request as too large
        for spec, _i, key, kid in self._candidates(task, self.chat_providers, prefer):
            if spec.name in skip_providers:
                continue
            tried += 1
            model = spec.model_for(task) or spec.chat_model
            url = f"{spec.base_url}/chat/completions"
            headers = {"Authorization": f"Bearer {key}", **spec.extra_headers}
            payload = {"model": model, "messages": messages, "temperature": temperature, "stream": True}
            yielded = False
            try:
                async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                    async with client.stream("POST", url, headers=headers, json=payload) as resp:
                        if resp.status_code != 200:
                            body = (await resp.aread()).decode("utf-8", "ignore")[:200]
                            raise httpx.HTTPStatusError(body, request=resp.request, response=resp)
                        self._bump(spec.name, kid)
                        async for line in resp.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                delta = json.loads(data)["choices"][0]["delta"].get("content")
                            except Exception:  # noqa: BLE001
                                continue
                            if delta:
                                yielded = True
                                yield delta
                if yielded:
                    return
                # 200 but empty — accept and stop.
                return
            except httpx.HTTPStatusError as e:
                last_err = e
                code = e.response.status_code
                blob = str(e).lower()
                # Payload too big for this provider — a 413, OR a 400 that's really "context length/
                # tokens per request" (the big-chat-payload case, e.g. a deep-research synthesis).
                # Either way every key rejects it identically, so skip the WHOLE provider for this
                # request and let a bigger-context one take it; the keys are healthy — don't trip
                # their breakers (that needlessly benched Groq for ordinary chat afterwards).
                too_large = code == 413 or (code == 400 and any(s in blob for s in (
                    "too large", "maximum context", "context length", "reduce", "tokens per",
                    "request_too_large")))
                if too_large:
                    skip_providers.add(spec.name)
                    logger.warning("%s rejected stream as too large (HTTP %d) — skipping it this turn",
                                   spec.name, code)
                    continue
                self._trip(spec.name, kid, _BREAKER_429 if code == 429 else _BREAKER_5XX)
                logger.warning("%s key %s -> HTTP %s, rotating", spec.name, kid, code)
                if yielded:
                    return  # can't cleanly restart mid-stream
            except Exception as e:  # noqa: BLE001
                last_err = e
                self._trip(spec.name, kid, _BREAKER_5XX)
                logger.warning("%s key %s failed (%s), rotating", spec.name, kid, type(e).__name__)
                if yielded:
                    return
        raise RuntimeError(f"All providers exhausted for task '{task}' (tried {tried}): {last_err}")

    async def chat(self, messages: list[dict], task: str = "chat", temperature: float = 0.5,
                   prefer: str = "") -> str:
        parts = [d async for d in self.chat_stream(messages, task=task, temperature=temperature,
                                                   prefer=prefer)]
        return "".join(parts).strip()

    # ------------------------------------------------------------------ #
    # Non-streaming completion with optional tool-calling (for the agent loop).
    # Returns the raw assistant message: {"content": str|None, "tool_calls": [...], "provider": str}.
    # Raises ToolUseFailed(blob) if a provider rejects the tool call as malformed.
    # ------------------------------------------------------------------ #
    async def chat_complete(
        self, messages: list[dict], tools: list[dict] | None = None,
        tool_choice: str | None = None, task: str = "chat",
        temperature: float = 0.4, max_tokens: int = 900,
    ) -> dict:
        last_err: Exception | None = None
        tool_use_blob: str | None = None
        # Start by skipping providers already known (this session) to be too small for a tool payload,
        # so a 65-tool action turn goes straight to one that fits instead of 413ing Groq first.
        skip_providers: set[str] = set(self._tools_too_big) if tools else set()
        for spec, _i, key, kid in self._candidates(task, self.chat_providers):
            if spec.name in skip_providers:
                continue
            base = spec.model_for(task) or spec.chat_model
            url = f"{spec.base_url}/chat/completions"
            headers = {"Authorization": f"Bearer {key}", **spec.extra_headers}
            # Primary model, plus ONE reliable fallback model (at temp 0) if a tool call
            # malforms. Kept to 2 attempts/key so a bad moment doesn't storm the rate limits.
            attempts = [(base, temperature)]
            if tools and spec.tool_fallbacks:
                attempts.append((spec.tool_fallbacks[0], 0.0))

            malformed_all = True
            transport_err = False
            for model, temp in attempts:
                payload: dict = {"model": model, "messages": messages,
                                 "temperature": temp, "max_tokens": max_tokens}
                if tools:
                    payload["tools"] = tools
                    payload["tool_choice"] = tool_choice or "auto"
                try:
                    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                        r = await client.post(url, headers=headers, json=payload)
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    self._trip(spec.name, kid, _BREAKER_5XX)
                    transport_err = True
                    break
                if r.status_code == 200:
                    self._bump(spec.name, kid)
                    msg = r.json()["choices"][0]["message"]
                    msg["provider"] = spec.name
                    return msg
                body = r.text[:500]
                if "tool_use_failed" in body or "failed_generation" in body:
                    tool_use_blob = body
                    continue                      # try next model
                # A 413 (payload too large) — or a 400 that's really "request too big" — is
                # DETERMINISTIC for this provider: the request exceeds its free-tier size cap, so
                # every sibling key would reject it identically. Skip the WHOLE provider for this
                # request (a provider with a bigger limit will take it) instead of 413-storming all
                # its keys. The keys are perfectly healthy, so DON'T trip their breakers — that was
                # needlessly disabling Groq for every later request too.
                blow = body.lower()
                too_large = r.status_code == 413 or (
                    r.status_code == 400 and any(s in blow for s in
                        ("too large", "maximum context", "context length", "reduce", "tokens per", "request_too_large")))
                if too_large:
                    skip_providers.add(spec.name)
                    last_err = RuntimeError(f"{spec.name} HTTP {r.status_code}: request too large — skipping provider")
                    logger.warning("%s rejected request as too large (HTTP %d) — skipping it this turn",
                                   spec.name, r.status_code)
                    malformed_all = False
                    break
                # genuine 429/5xx — stop trying this provider
                secs = _breaker_secs(r) if r.status_code == 429 else _BREAKER_5XX
                self._trip(spec.name, kid, secs)
                last_err = RuntimeError(f"{spec.name} HTTP {r.status_code}: {body[:120]}")
                malformed_all = False
                break
            else:
                # every attempt malformed the tool call
                if malformed_all:
                    skip_providers.add(spec.name)
                    logger.warning("%s malformed tool call across %d models — next provider",
                                   spec.name, len(attempts))
            if transport_err:
                continue
        if tool_use_blob is not None:
            raise ToolUseFailed(tool_use_blob)
        raise RuntimeError(f"chat_complete: all providers failed: {last_err}")

    async def vision(self, prompt: str, image_data_url: str, temperature: float = 0.3,
                     prefer: str = "") -> str:
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ],
        }]
        return await self.chat(messages, task="vision", temperature=temperature, prefer=prefer)

    # ------------------------------------------------------------------ #
    # Embeddings (OpenAI-compatible /embeddings)
    # ------------------------------------------------------------------ #
    async def embed(self, texts: list[str]) -> list[list[float]]:
        last_err: Exception | None = None
        for spec, _i, key, kid in self._candidates("embed", self.chat_providers):
            url = f"{spec.base_url}/embeddings"
            headers = {"Authorization": f"Bearer {key}", **spec.extra_headers}
            try:
                async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                    r = await client.post(url, headers=headers, json={"model": spec.embed_model, "input": texts})
                    r.raise_for_status()
                    self._bump(spec.name, kid)
                    return [d["embedding"] for d in r.json()["data"]]
            except Exception as e:  # noqa: BLE001
                last_err = e
                self._trip(spec.name, kid, _BREAKER_5XX)
        raise RuntimeError(f"All embed providers failed: {last_err}")

    # ------------------------------------------------------------------ #
    # STT — Groq Whisper (OpenAI-compat multipart) + Deepgram (sync)
    # ------------------------------------------------------------------ #
    def transcribe(self, wav_bytes: bytes) -> str:
        last_err: Exception | None = None
        for spec in self.stt_providers:
            for key in spec.keys:
                kid = _key_id(key)
                if self._blocked(spec.name, kid) or self._count(spec.name, kid) >= spec.daily_limit:
                    continue
                try:
                    if spec.name == "deepgram":
                        text = self._deepgram(key, spec.model, wav_bytes)
                    else:  # groq / any OpenAI-compat audio endpoint
                        text = self._whisper_openai(key, spec.model, wav_bytes)
                    self._bump(spec.name, kid)
                    return text
                except httpx.HTTPStatusError as e:
                    last_err = e
                    self._trip(spec.name, kid, _BREAKER_429 if e.response.status_code == 429 else _BREAKER_5XX)
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    self._trip(spec.name, kid, _BREAKER_5XX)
        raise RuntimeError(f"All STT providers failed: {last_err}")

    @staticmethod
    def _whisper_openai(key: str, model: str, wav: bytes) -> str:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as c:
            r = c.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("audio.wav", wav, "audio/wav")},
                data={"model": model, "language": "en", "temperature": "0"},
            )
            r.raise_for_status()
            return (r.json().get("text") or "").strip()

    @staticmethod
    def _deepgram(key: str, model: str, wav: bytes) -> str:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as c:
            r = c.post(
                f"https://api.deepgram.com/v1/listen?model={model}&smart_format=true&language=en",
                headers={"Authorization": f"Token {key}", "Content-Type": "audio/wav"},
                content=wav,
            )
            r.raise_for_status()
            alt = r.json()["results"]["channels"][0]["alternatives"][0]
            return (alt.get("transcript") or "").strip()

    # ------------------------------------------------------------------ #
    def stats(self) -> dict:
        out = {"day": _today(), "providers": []}
        for spec in all_specs():
            keys = []
            for key in spec.keys:
                kid = _key_id(key)
                keys.append({
                    "key_id": kid,
                    "used_today": self._count(spec.name, kid),
                    "daily_limit": spec.daily_limit,
                    "blocked": self._blocked(spec.name, kid),
                })
            out["providers"].append({
                "name": spec.name, "active": spec.active, "priority": spec.priority,
                "tasks": sorted(spec.tasks), "keys": keys,
            })
        # STT providers too
        out["stt"] = []
        for spec in active_stt_providers():
            out["stt"].append({
                "name": spec.name,
                "keys": [{"key_id": _key_id(k), "used_today": self._count(spec.name, _key_id(k)),
                          "daily_limit": spec.daily_limit} for k in spec.keys],
            })
        return out


_singleton: KeyRotator | None = None


def get_rotator() -> KeyRotator:
    global _singleton
    if _singleton is None:
        _singleton = KeyRotator()
    return _singleton
