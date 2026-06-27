"""
Memory consolidation — the nightly 03:00 job (and on-demand).

Reads the last 24 h of raw conversational turns and, via one free LLM call (key rotator),
produces three things:
  1. a single episodic SUMMARY of the day  -> stored back into episodic memory (kind=summary)
  2. durable SEMANTIC FACTS (key/value)    -> upserted into the semantic store
  3. knowledge-graph TRIPLES               -> added to the graph

This is what lets JARVIS carry context across days without bloating working memory: the
raw turns stay searchable, but the distilled facts/triples/summary are what he leans on.

Everything runs on free providers and degrades gracefully — if the LLM call fails or
returns junk, the day's raw turns are untouched and we simply try again tomorrow.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import re

logger = logging.getLogger("jarvis.memory.consolidate")

_CONSOLIDATE_PROMPT = """You are the memory consolidator for JARVIS, a personal AI assistant.
Below is a transcript of the last day's conversation between JARVIS and his user (the user
is the boss; assistant lines are JARVIS). Distill DURABLE memory from it.

Return ONLY a JSON object (no prose, no markdown fences) with exactly these keys:
{
  "summary": "2-4 sentence recap of what actually happened / was discussed today",
  "facts": [ {"key": "dotted.snake_case", "value": "short value"} ],
  "triples": [ {"subject": "entity", "predicate": "relation", "object": "entity"} ]
}

Rules:
- "facts" = durable truths about the USER or his world worth remembering forever:
  user.full_name, user.location, prefs.<thing>, contacts.<name>.relation,
  contacts.<name>.phone, routines.<name>, project.<name>.status, etc. Lowercase dotted keys.
- Only include facts you are confident are stable. Skip one-off chit-chat, transient state,
  weather, timers, and anything JARVIS merely searched for. If none, use an empty array.
- "triples" = (subject, predicate, object) relationships, lowercase, e.g.
  {"subject":"aditya","predicate":"works_on","object":"project_atlas"}.
- Keep it tight. Quality over quantity. Empty arrays are fine.

TRANSCRIPT:
"""


def _strip_json(text: str) -> dict | None:
    if not text:
        return None
    # tolerate ```json fences or leading prose
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


class Consolidator:
    def __init__(self, episodic, semantic, graph, rotator) -> None:
        self.episodic = episodic
        self.semantic = semantic
        self.graph = graph
        self.rotator = rotator
        self._task: asyncio.Task | None = None

    # ---- the actual distillation ----------------------------------------- #
    async def consolidate(self, hours: float = 24.0) -> dict:
        episodes = await asyncio.to_thread(self.episodic.recent, hours, kinds=("turn",))
        if len(episodes) < 2:
            logger.info("consolidation skipped — only %d turns in the last %gh", len(episodes), hours)
            return {"summary": None, "facts": 0, "triples": 0}

        transcript = "\n".join(
            f"{'JARVIS' if e.role == 'assistant' else 'User'}: {e.text}" for e in episodes
        )[:12000]
        messages = [{"role": "user", "content": _CONSOLIDATE_PROMPT + transcript}]
        try:
            raw = await self.rotator.chat(messages, temperature=0.2)
        except Exception as e:  # noqa: BLE001
            logger.warning("consolidation LLM call failed (%s) — will retry next cycle", e)
            return {"summary": None, "facts": 0, "triples": 0, "error": str(e)}

        data = _strip_json(raw)
        if not data:
            logger.warning("consolidation returned unparseable output — skipping")
            return {"summary": None, "facts": 0, "triples": 0, "error": "unparseable"}

        n_facts = n_triples = 0
        summary = (data.get("summary") or "").strip()
        if summary:
            day = _dt.date.today().isoformat()
            await asyncio.to_thread(
                self.episodic.add, f"[Day summary {day}] {summary}",
                kind="summary", role="system", channel="consolidation")

        for f in data.get("facts") or []:
            if isinstance(f, dict) and f.get("key") and f.get("value"):
                await asyncio.to_thread(self.semantic.set, str(f["key"]), str(f["value"]), "consolidation")
                n_facts += 1

        for t in data.get("triples") or []:
            if isinstance(t, dict) and t.get("subject") and t.get("predicate") and t.get("object"):
                await asyncio.to_thread(self.graph.add_triple,
                                        str(t["subject"]), str(t["predicate"]), str(t["object"]))
                n_triples += 1

        logger.info("consolidation done: summary=%s, +%d facts, +%d triples",
                    bool(summary), n_facts, n_triples)
        return {"summary": summary or None, "facts": n_facts, "triples": n_triples}

    # ---- nightly scheduler ----------------------------------------------- #
    @staticmethod
    def _seconds_until(hour: int, minute: int) -> float:
        now = _dt.datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += _dt.timedelta(days=1)
        return (target - now).total_seconds()

    def start(self, hour: int = 3, minute: int = 0) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(hour, minute))
        logger.info("nightly memory consolidation scheduled for %02d:%02d", hour, minute)

    async def _loop(self, hour: int, minute: int) -> None:
        while True:
            try:
                await asyncio.sleep(self._seconds_until(hour, minute))
                await self.consolidate(hours=24.0)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception("nightly consolidation cycle failed")
                await asyncio.sleep(3600)  # back off an hour, then resume the schedule
