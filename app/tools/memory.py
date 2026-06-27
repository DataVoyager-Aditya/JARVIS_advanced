"""Memory tools (Phase 4) — let JARVIS deliberately store and look up durable memory.

Passive recall is already injected into every turn's prompt (see MemoryService.context_block),
so these are for the explicit cases: the boss says "remember that..." or asks "what did I tell
you about...". Both back onto the same persistent 3-tier store.
"""

from __future__ import annotations

from config import DEFAULT_CHANNEL
from app.services.memory import get_memory
from app.tools import tool


@tool(
    "Commit a durable fact the USER STATES about himself or his world to long-term memory (his "
    "name, where he lives, a preference, a person/contact, a routine, an ongoing project). Only "
    "use it when he TELLS you a lasting fact and it's worth recalling weeks later. "
    "NEVER use it to log that he asked a question, searched for something, or for transient "
    "things (timers, today's weather, one-off requests, what he just looked up). When unsure, "
    "do NOT remember. Every conversation is already saved automatically — this is only for "
    "standout, durable facts. Do not announce it; a quiet 'Noted' is enough.",
    params={
        "fact": {"type": "string",
                 "description": "the fact to remember, as a clear standalone sentence, "
                                "e.g. 'Aditya takes his coffee black, no sugar.'"},
        "key": {"type": "string",
                "description": "optional canonical dotted key for a single durable value, "
                               "e.g. 'prefs.coffee_order' or 'contacts.vikram.relation'. "
                               "Omit for general facts."},
    },
    required=["fact"],
    narration="Committing that to memory",
)
def remember(fact: str, key: str = "") -> str:
    return get_memory().remember_fact(fact, key=key or None, channel=DEFAULT_CHANNEL)


@tool(
    "Search your long-term memory for something from the past — what the user told you "
    "earlier, a person, a project, a preference, or any prior conversation. Use when he "
    "refers back to something and you don't already have it in front of you.",
    params={
        "query": {"type": "string", "description": "what to look up, e.g. 'my brother' or "
                                                    "'project Atlas' or 'coffee order'"},
        "since_hours": {"type": "number",
                        "description": "optional: only recall things from the last N hours"},
    },
    required=["query"],
    narration="Checking my memory",
)
def recall(query: str, since_hours: float = 0.0) -> str:
    return get_memory().recall(query, since_hours=since_hours or None)
