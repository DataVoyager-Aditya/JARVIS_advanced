"""
Tool registry — JARVIS's hands.

A tool is a plain function decorated with @tool that returns a short string (the result
fed back to the model). The decorator records an OpenAI-compatible function schema so the
agent can call it natively.

    @tool(
        "Search the web for current information.",
        params={"query": {"type": "string", "description": "what to search for"}},
        required=["query"],
        narration="Searching the web",
    )
    def web_search(query: str) -> str:
        ...

`discover()` imports every sibling module so their @tool registrations run. `for_openai()`
returns the schema list for the chat API. Tools declare a `tier` (owner/trusted/guest) for
the Phase 11 access-control layer; ignored until then.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import re
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("jarvis.tools")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., str]
    narration: str = ""          # short present-tense status spoken while it runs
    tier: str = "owner"          # min trust tier (Phase 11); unused for now
    terminal: bool = False       # result is already a complete, speakable confirmation — the
                                 # agent may speak it directly and skip the extra "rephrase" LLM
                                 # turn (the big latency cut for send/delete/like/etc. actions)

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def run(self, args: dict[str, Any]) -> str:
        # Be robust to the model: None/non-dict args -> {}, and drop any hallucinated
        # keys the function doesn't declare.
        if not isinstance(args, dict):
            args = {}
        allowed = set(self.parameters.get("properties", {}).keys())
        clean = {k: v for k, v in args.items() if k in allowed}
        result = self.func(**clean)
        return "" if result is None else str(result)


_REGISTRY: dict[str, Tool] = {}


def tool(description: str, params: dict | None = None, required: list[str] | None = None,
         narration: str = "", tier: str = "owner", terminal: bool = False) -> Callable:
    def deco(func: Callable[..., str]) -> Callable[..., str]:
        schema = {"type": "object", "properties": params or {}, "required": required or []}
        _REGISTRY[func.__name__] = Tool(
            name=func.__name__, description=description, parameters=schema,
            func=func, narration=narration, tier=tier, terminal=terminal,
        )
        return func
    return deco


def get(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def all_tools() -> list[Tool]:
    return list(_REGISTRY.values())


# A small set always offered (cheap, ubiquitous), so a vague ask never finds zero tools.
_CORE_TOOLS = {"web_search", "recall", "remember", "get_weather", "open_app", "open_url"}
# Filler words that must NOT cause a tool to be considered "relevant" (they appear in many
# descriptions). Without this, "what's the..." matched half the registry on incidental overlap.
_STOP = {"the", "and", "you", "your", "that", "this", "for", "are", "can", "what", "who", "how",
         "with", "have", "has", "not", "was", "will", "but", "they", "them", "from", "get", "got",
         "its", "our", "out", "now", "any", "all", "one", "let", "like", "just", "know", "saying",
         "also", "want", "need", "please", "jarvis", "sir", "hey", "there", "here", "would", "could",
         "about", "tell", "give", "make", "some", "thing", "things", "okay", "yeah", "yes", "his",
         "her", "him", "she", "their", "been", "were", "does", "did", "say", "said", "ask", "asked"}
# Hand-tuned cue words -> tools, for cases where the request's words don't literally appear in the
# tool name/description (synonyms). Keeps recall high so JARVIS rarely "can't do that".
_TOOL_CUES: dict[str, tuple[str, ...]] = {
    "message": ("send_whatsapp", "read_whatsapp", "read_whatsapp_chat", "compose_reply", "reply_to_messages"),
    "whatsapp": ("send_whatsapp", "read_whatsapp", "read_whatsapp_chat", "mark_whatsapp_read", "compose_reply"),
    "text": ("send_whatsapp", "compose_reply"),
    "dm": ("send_instagram_dm", "instagram_activity"),
    "insta": ("instagram_activity", "send_instagram_dm", "instagram_post", "instagram_profile"),
    "mail": ("read_emails", "send_email", "reply_email"),
    "email": ("read_emails", "send_email", "reply_email"),
    "inbox": ("unified_inbox", "read_emails", "read_whatsapp"),
    "call": ("place_call", "phone_call_action"), "dial": ("place_call",), "phone": ("place_call", "phone_call_action"),
    "ring": ("place_call",), "missed": ("phone_call_action",),
    "remind": ("set_reminder", "list_reminders", "cancel_reminders"),
    "timer": ("set_timer",), "alarm": ("stop_alarm", "snooze_alarm", "set_timer"),
    "note": ("note_write", "note_search", "note_today"),
    "routine": ("set_routine", "save_routine", "run_routine", "list_routines"),
    "gym": ("set_routine",), "watch": ("watch", "unwatch", "list_watches", "watch_topic"),
    "monitor": ("watch_topic", "watch"), "eye": ("watch_topic", "watch"),
    "track": ("watch", "list_watches", "watch_topic"), "stock": ("market_check", "watch"),
    "crypto": ("market_check", "watch"),
    "price": ("market_check",), "news": ("whats_happening", "watch", "web_search"),
    "brief": ("whats_happening", "read_briefing"), "happening": ("whats_happening",),
    "research": ("deep_research", "research_status", "read_briefing", "watch_topic"),
    "sweep": ("deep_research", "research_status"), "dig": ("deep_research",),
    "investigate": ("deep_research",), "deep": ("deep_research",),
    "briefing": ("read_briefing", "research_status"), "dive": ("deep_research",),
    "findings": ("read_briefing", "research_status"),
    "screen": ("read_screen", "take_screenshot"), "see": ("look", "read_screen", "describe_image"),
    "look": ("look", "describe_image"), "camera": ("look",), "photo": ("look", "describe_image"),
    "play": ("play_youtube", "media_control"), "music": ("play_youtube", "media_control"),
    "video": ("play_youtube",), "youtube": ("play_youtube",), "volume": ("set_volume", "media_control"),
    "pause": ("media_control",), "mute": ("set_volume", "media_control"),
    "remember": ("remember",), "recall": ("recall",), "forget": ("clear_call_log",),
    "weather": ("get_weather",), "search": ("web_search",), "google": ("web_search",),
    "open": ("open_app", "open_url"), "launch": ("open_app",),
    "mute_chat": ("mute_chat", "unmute_chat", "list_muted"),
    "access": ("list_access", "remove_access", "enroll_person", "reverify_user"),
    "enroll": ("enroll_person",), "add": ("enroll_person", "set_routine", "watch"),
    "quiet": ("proactive_control",), "chime": ("proactive_control",),
}

_WORD_RE = re.compile(r"[a-z][a-z0-9_]{2,}")


def for_openai(allow: Callable[[Tool], bool] | None = None,
               relevant_to: str | None = None, max_tools: int = 14) -> list[dict]:
    """OpenAI tool schemas. With `allow`, only tools the predicate accepts are exposed (the Phase-11
    access layer passes one so a below-tier speaker never even sees owner-only tools).

    With `relevant_to` (the user's utterance), returns only the tools likely relevant — the core set
    plus those whose name/description/cues overlap the request — capped at `max_tools`. This keeps the
    request small enough for the fast free providers (Groq's per-request cap was 413-ing on all 68
    tools at once) WITHOUT touching the persona or memory. Without `relevant_to`, returns everything."""
    avail = [t for t in _REGISTRY.values() if allow is None or allow(t)]
    if not relevant_to:
        return [t.to_openai() for t in avail]

    words = {w for w in _WORD_RE.findall(relevant_to.lower()) if w not in _STOP}
    cued: set[str] = set()
    for w in words:
        cued.update(_TOOL_CUES.get(w, ()))

    def score(t: Tool) -> int:
        name = t.name.lower()
        s = 6 if t.name in cued else 0
        if any(w in name for w in words):                 # a request word appears in the tool name
            s += 5
        desc_words = set(_WORD_RE.findall((t.name + " " + t.description).lower())) - _STOP
        s += min(2, len(words & desc_words))              # capped description overlap (a weak signal)
        return s

    ranked = sorted(avail, key=lambda t: (t.name not in _CORE_TOOLS, -score(t), t.name))
    chosen: list[Tool] = []
    for t in ranked:
        if len(chosen) >= max_tools:
            break
        if t.name in _CORE_TOOLS or score(t) > 0:
            chosen.append(t)
    return [t.to_openai() for t in chosen]


# --- Phase 11 access policy ------------------------------------------------------------- #
# The ONE auditable place where tools are opened BELOW owner. Everything not listed stays
# owner-only (the secure default). `owner+passphrase` is declared on the tool itself.
#   guest   — pure public-info Q&A, no side effects, no personal data
#   trusted — harmless, self-scoped actions a family member/close friend may use
_TIER_POLICY: dict[str, set[str]] = {
    "guest":   {"web_search", "get_weather"},
    "trusted": {"set_timer", "play_youtube", "media_control", "set_volume",
                "stop_alarm", "snooze_alarm"},
}


def _apply_tier_policy() -> None:
    for tier, names in _TIER_POLICY.items():
        for n in names:
            t = _REGISTRY.get(n)
            if t and t.tier == "owner":      # never override an explicit owner+passphrase tool
                t.tier = tier


_discovered = False


def discover() -> list[Tool]:
    """Import every tool module in this package so their @tool decorators register."""
    global _discovered
    if _discovered:
        return all_tools()
    import app.tools as pkg
    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"app.tools.{mod.name}")
        except Exception as e:  # noqa: BLE001
            logger.warning("tool module '%s' failed to import: %s", mod.name, e)
    _apply_tier_policy()
    _discovered = True
    logger.info("Tools loaded: %s", ", ".join(sorted(t.name for t in all_tools())) or "none")
    return all_tools()
