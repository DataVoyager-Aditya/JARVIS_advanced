"""
Deep-research tools (Phase 10.A) — JARVIS goes off and investigates, by voice.

  deep_research        — start a full background sweep on a topic (returns at once; he keeps talking).
  research_status      — "how's that research going / is it done yet?"
  read_briefing        — read back the findings of a finished sweep (latest, or a named topic).
  watch_topic / unwatch_topic / list_research_topics — continuous topic monitoring.

deep_research / status / read are NON-terminal (JARVIS phrases the result in character); the watch
management tools are terminal (their confirmation is already a complete, speakable line).
"""

from __future__ import annotations

import re

from app.tools import tool
from app.services import research


@tool(
    "Start a FULL deep-research sweep on a topic — the long, thorough investigation where you go off, "
    "read many sources across the web, cross-reference them, and come back with a synthesized "
    "briefing. Use for 'do a full sweep / deep dive / proper research on X', 'dig into X', 'research "
    "X thoroughly', 'give me a full briefing on X'. This is NOT for a quick fact (use web_search for "
    "that) — it runs in the BACKGROUND and takes a little while. After calling it, tell him you're on "
    "it in one warm line and carry on; you'll surface progress and the finished briefing yourself.",
    params={"topic": {"type": "string", "description": "what to research, in his words"}},
    required=["topic"],
    narration="Starting a deep sweep",
)
def deep_research(topic: str) -> str:
    if not research.search_available():
        return ("Deep research isn't available right now — it needs a free Tavily search key in the "
                ".env. Say so honestly, in character, and offer a quick web_search instead.")
    res = research.start_research(topic)
    status = res.get("status")
    if status == "started":
        return (f"A deep-research sweep on '{topic}' is now running in the background. Tell him you're "
                "on it and you'll bring the full briefing shortly — in ONE warm line — then carry on. "
                "Do NOT pretend it's finished or invent any findings; there are none yet.")
    if status == "already_running":
        return (f"You're already researching '{topic}' right now — tell him it's already underway and "
                "you'll have it shortly. Don't start it again.")
    if status == "busy":
        return (f"You already have other sweeps running ({res.get('message','')}) and can't start "
                "another yet — tell him that plainly and offer to queue this once one finishes.")
    return ("The research engine isn't available right now (it needs a free Tavily search key) — say "
            "so honestly and offer a quick web_search instead.")


@tool(
    "Check on a deep-research sweep already running — 'how's that research going', 'is my briefing "
    "ready', 'where's the deep dive on X', 'what are you working on'. Reports what's still running "
    "(and how long) and the most recent finished briefing.",
    narration="Checking the research",
)
def research_status() -> str:
    st = research.research_status()
    active = st.get("active") or []
    parts = []
    if active:
        for a in active:
            mins = max(1, a["elapsed_s"] // 60)
            prog = f" — {a['progress']}" if a.get("progress") else ""
            parts.append(f"still digging into '{a['topic']}' (about {mins} min in){prog}")
    last = st.get("last")
    if last and last.get("ok"):
        parts.append(f"the last finished briefing was on '{last['topic']}'")
    if not parts:
        return ("Nothing is researching right now and there's no finished briefing yet — tell him "
                "he can ask you to dig into anything.")
    return ("Report this to him naturally, in character, most useful first: " + "; ".join(parts) + ".")


@tool(
    "Read back the findings of a finished deep-research sweep — use when he asks 'what did you find "
    "on X', 'read me that briefing', 'what's the result of the research', 'tell me about your deep "
    "dive'. Leave topic empty for the most recent briefing. Deliver it as a tight spoken digest: the "
    "headline first, then the few key facts and any contradiction, and how confident you are — never "
    "a wall of text. Everything here is real and sourced; if there's no briefing yet, say so.",
    params={"topic": {"type": "string", "description": "the topic to read back; empty = most recent"}},
    narration="Pulling up the briefing",
)
def read_briefing(topic: str = "") -> str:
    b = research.get_briefing(topic) if topic.strip() else research.latest_briefing()
    if not b:
        if topic.strip():
            return (f"You don't have a finished briefing on '{topic}' — tell him that, and offer to "
                    "run a deep sweep on it now.")
        return ("There's no finished briefing yet — tell him to ask you to research something and "
                "you'll dig in.")
    findings = _key_findings(b.full_md, limit=4)
    out = [f"Briefing on {b.title}.", b.summary.strip()]
    if findings:
        out.append("Key points: " + findings)
    if b.confidence and b.confidence.lower() not in ("none", ""):
        out.append(f"Confidence: {b.confidence}.")
    n = len(b.sources or [])
    if n:
        out.append(f"({n} sources.)")
    digest = " ".join(p for p in out if p).strip()
    # Keep the whole return under the runner's 900-char tool-result cap so the digest (with its
    # confidence + source count) isn't truncated mid-thought.
    return ("Deliver as a tight spoken digest, headline first (don't read verbatim):\n" + digest[:800])


@tool(
    "Keep watching a TOPIC over time and flag a genuine development on it — use for 'keep an eye on "
    "X', 'track developments on X', 'watch this topic', 'follow what happens with X'. JARVIS re-runs "
    "the research on his own and only speaks up when something materially changes. (This is for a "
    "topic evolving over time — for a stock/crypto price use the watch tool instead.)",
    params={"topic": {"type": "string", "description": "the topic to keep monitoring"}},
    required=["topic"],
    narration="Setting a topic watch",
    terminal=True,
)
def watch_topic(topic: str) -> str:
    res = research.watch_topic(topic)
    if not res.get("ok"):
        return res.get("message", "I couldn't set that watch, sir.")
    return (f"I'll keep watching {topic} and flag anything that genuinely develops, sir — "
            "you'll hear from me when it matters.")


@tool(
    "Stop continuously watching a research topic — 'stop tracking X', 'you can stop watching X', "
    "'drop the watch on X'.",
    params={"topic": {"type": "string", "description": "the topic to stop monitoring"}},
    required=["topic"],
    narration="Updating topic watches",
    terminal=True,
)
def unwatch_topic(topic: str) -> str:
    n = research.unwatch_topic(topic)
    return (f"Done, sir — I've stopped watching {topic}." if n else
            f"I wasn't tracking {topic}, sir.")


@tool(
    "List the research topics JARVIS is continuously monitoring (and recent briefings) — 'what are "
    "you keeping an eye on', 'what topics are you tracking', 'what have you researched'.",
    narration="Checking topic watches",
    terminal=True,
)
def list_research_topics() -> str:
    mons = research.topics()
    briefs = research.list_briefings(limit=5)
    parts = []
    if mons:
        parts.append("Tracking: " + ", ".join(m.label for m in mons))
    if briefs:
        parts.append("recent briefings on " + ", ".join(b.title for b in briefs))
    if not parts:
        return ("You're not tracking any research topics yet, sir, and there are no briefings — say "
                "the word and I'll dig into something.")
    return ("On the research side, sir — " + "; ".join(parts) + ".")


def _key_findings(full_md: str, limit: int = 4) -> str:
    """Pull the KEY FINDINGS bullets out of the stored briefing, compacted onto one line."""
    if not full_md:
        return ""
    m = re.search(r"KEY FINDINGS\s*\n(.+?)(?:\n\s*\n[A-Z#]|CONTRADICTIONS|CONFIDENCE|## SOURCES|$)",
                  full_md, re.DOTALL | re.IGNORECASE)
    if not m:
        return ""
    bullets = []
    for ln in m.group(1).splitlines():
        ln = re.sub(r"^\s*[-*•]\s*", "", ln).strip()
        ln = re.sub(r"\s*\[\d+(?:\]\[\d+)*\]", "", ln)   # strip citations for the spoken form
        if ln:
            bullets.append(ln)
        if len(bullets) >= limit:
            break
    return " ".join(bullets)
