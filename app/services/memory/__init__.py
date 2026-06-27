"""
Phase 4 — JARVIS's memory layer (3 tiers + knowledge graph).

  Tier 1  Working memory   — the live conversation (handled by the listener's history).
  Tier 2  Episodic         — every turn vectorized into FAISS (episodic.py).
  Tier 3  Semantic facts   — durable key/value he should always know (semantic.py).
  Graph   Entities/relations — structured "tell me about X" (graph.py).
  Job     Consolidator     — nightly 03:00 distillation (consolidator.py).

`MemoryService` is the single facade the rest of JARVIS talks to:
  - log_turn(...)        store a finished exchange into episodic memory
  - context_block(...)   the passive-recall string injected into the system prompt each turn
  - recall(...)          explicit search (backs the recall tool)
  - remember_fact(...)   store a durable fact (backs the remember tool)

All blocking work (embedding, FAISS, SQLite) is wrapped in asyncio.to_thread by callers in
the async path. The synchronous methods here are safe to call from worker threads.
"""

from __future__ import annotations

import asyncio
import logging

from config import (MEMORY_DB, EPISODIC_DB, EPISODIC_INDEX, PROFILE_PATH, DEFAULT_CHANNEL,
                    USER_ADDRESS, JARVIS_USER_NAME)
from .episodic import EpisodicStore, Episode
from .semantic import SemanticStore
from .graph import KnowledgeGraph
from .profile import load_profile

logger = logging.getLogger("jarvis.memory")

# Entity words that aren't worth a knowledge-graph lookup when scanning a question.
_STOP = {"the", "a", "an", "my", "your", "his", "her", "their", "what", "who", "when",
         "where", "why", "how", "is", "are", "was", "were", "do", "does", "did", "tell",
         "me", "about", "of", "to", "and", "for", "in", "on", "with", "i", "you", "it",
         "that", "this", "have", "has", "going", "whats", "hows"}


class MemoryService:
    def __init__(self) -> None:
        self.episodic = EpisodicStore(EPISODIC_DB, EPISODIC_INDEX)
        self.semantic = SemanticStore(MEMORY_DB)
        self.graph = KnowledgeGraph(MEMORY_DB)
        self._consolidator = None  # lazily built (needs the rotator)
        # Seed durable facts from the human-editable profile (file is the source of truth).
        load_profile(self.semantic, PROFILE_PATH)
        logger.info("memory online — %d episodes, %d facts, %d/%d graph",
                    self.episodic.count(), self.semantic.count(), *self.graph.counts())

    # ---- writes ----------------------------------------------------------- #
    def log_turn(self, user_text: str, assistant_text: str,
                 channel: str = DEFAULT_CHANNEL) -> None:
        """Store a completed exchange (both sides) into episodic memory."""
        if user_text:
            self.episodic.add(user_text, kind="turn", role="user", channel=channel)
        if assistant_text:
            self.episodic.add(assistant_text, kind="turn", role="assistant", channel=channel)

    def remember_fact(self, fact: str, key: str | None = None,
                      channel: str = DEFAULT_CHANNEL) -> str:
        """Back the `remember` tool: store a durable fact in episodic memory (always
        searchable) and, when a clean key is supplied, also as a canonical semantic fact."""
        fact = (fact or "").strip()
        if not fact:
            return "Nothing to remember."
        self.episodic.add(fact, kind="fact", role="memory", channel=channel)
        if key:
            self.semantic.set(key, fact, source="tool")
        return "Committed to memory."

    # ---- reads ------------------------------------------------------------ #
    def recall(self, query: str, k: int = 6, since_hours: float | None = None) -> str:
        """Back the `recall` tool: semantic episodic search + matching semantic facts +
        any knowledge-graph entity. Returns a compact, ready-to-speak digest."""
        query = (query or "").strip()
        if not query:
            return "What would you like me to recall?"
        since = None
        if since_hours:
            import datetime as _dt
            since = _dt.datetime.now().timestamp() - since_hours * 3600

        chunks: list[str] = []

        facts = self.semantic.search(query)
        if facts:
            chunks.append("Known: " + "; ".join(f"{f.key.replace('_',' ')} = {f.value}"
                                                 for f in facts[:6]))
        for ent in self._entity_candidates(query):
            desc = self.graph.describe(ent)
            if desc:
                chunks.append(desc)

        eps = self.episodic.search(query, k=k, since=since)
        for e in eps:
            who = "You" if e.role in ("user", "memory") else "I"
            chunks.append(f"{who} ({e.when}): {e.text}")

        if not chunks:
            return f"I don't have anything on '{query}' in memory."
        return "\n".join(chunks[: k + 4])

    def context_block(self, user_text: str, channel: str = DEFAULT_CHANNEL,
                      k: int = 4) -> str:
        """Passive recall injected into the system prompt every turn: the canonical facts
        JARVIS should always know, plus a few memories relevant to THIS message."""
        sections: list[str] = []

        # Cap the fact dump so the system prompt stays lean (the full profile every turn was
        # bloating the request). 120 lines is plenty for the durable essentials.
        facts = self.semantic.as_prompt_lines(limit=120)
        if facts:
            sections.append("WHAT YOU KNOW ABOUT HIM (durable memory):\n" + "\n".join(facts))

        # Passive injection uses a slightly higher floor than explicit recall to keep the
        # prompt clean — only surface memories with a real connection to this message.
        relevant = self.episodic.search(user_text, k=k, min_score=0.22)
        # Don't echo the user's own current message back as a "memory".
        relevant = [e for e in relevant if e.text.strip().lower() != user_text.strip().lower()]
        if relevant:
            lines = []
            for e in relevant:
                who = "he said" if e.role in ("user", "memory") else "you replied"
                # Truncate long recalled turns (e.g. an old inbox digest) so they don't bloat.
                txt = e.text if len(e.text) <= 200 else e.text[:200] + "…"
                lines.append(f"- ({e.when}, {who}): {txt}")
            sections.append("POSSIBLY RELEVANT MEMORIES (from earlier; use only if pertinent):\n"
                            + "\n".join(lines))

        return "\n\n".join(sections)

    def _entity_candidates(self, text: str) -> list[str]:
        words = [w.strip(".,!?'\"").lower() for w in text.split()]
        return [w for w in words if len(w) > 2 and w not in _STOP][:6]

    # ---- consolidation ---------------------------------------------------- #
    def _ensure_consolidator(self):
        if self._consolidator is None:
            from app.services.llm.key_rotator import get_rotator
            from .consolidator import Consolidator
            self._consolidator = Consolidator(self.episodic, self.semantic,
                                              self.graph, get_rotator())
        return self._consolidator

    def start_consolidation(self, hour: int = 3, minute: int = 0) -> None:
        try:
            self._ensure_consolidator().start(hour, minute)
        except RuntimeError:
            # no running event loop yet — caller should invoke from within the loop
            logger.debug("start_consolidation called outside an event loop")

    async def consolidate_now(self, hours: float = 24.0) -> dict:
        return await self._ensure_consolidator().consolidate(hours)

    def stats(self) -> dict:
        e, r = self.graph.counts()
        return {"episodes": self.episodic.count(), "facts": self.semantic.count(),
                "entities": e, "relations": r,
                "embeddings": self.episodic._embed.available()}


_memory: MemoryService | None = None
_memory_lock = __import__("threading").Lock()


def get_memory() -> MemoryService:
    # Double-checked locking: the backend warms memory in a thread while the first request
    # may also call this on the event loop — without the lock both would build it (the
    # "loaded twice" bug). The lock makes exactly one instance.
    global _memory
    if _memory is None:
        with _memory_lock:
            if _memory is None:
                _memory = MemoryService()
    return _memory
