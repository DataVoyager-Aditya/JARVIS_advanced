"""
AgentRunner — JARVIS's tool-using agent loop (Phase 6 core).

Native OpenAI-compatible function calling via the KeyRotator. Loop:
  1. Send (persona + history + user) with the tool schema.
  2. If the reply has tool_calls, run each (narrating a short status aloud), append results.
  3. Repeat until the model returns plain text — capped at MAX_STEPS.
  4. Return the final text + a trace of what ran.

Multi-step command chaining ("open YouTube, then Telegram, then play Money Heist") falls
out of this naturally: the model emits several tool calls / several loop turns in order.

Two hard-won guards (carried over from the first JARVIS build):
  - **Hallucination guard:** if the model says it DID something ("opening YouTube", "timer
    set") but called NO tool, the action didn't happen — we force a real tool call. This is
    what stops the "your coffee is already brewing" fakery when no tool actually ran.
  - **Malformed-call recovery:** some models emit a legacy <function=name{...}> blob the API
    rejects; we parse and run it instead of letting the model pretend it worked.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from config import build_system_prompt, MEMORY_ENABLED, DEFAULT_CHANNEL, EMOTION_ENABLED
from app.services.llm.key_rotator import get_rotator, ToolUseFailed
from app.services import identity
import app.tools as tools

if MEMORY_ENABLED:
    from app.services.memory import get_memory
if EMOTION_ENABLED:
    import app.services.emotion as emotion

logger = logging.getLogger("jarvis.agent")

MAX_STEPS = 6
MAX_TOOL_RESULT_CHARS = 900   # keep tool outputs compact so multi-step turns don't bloat the
                              # request (big payloads were getting 413'd, forcing slow retries)

# Verbs that imply the model CLAIMED an action. In a tool-less reply with no prior tool
# run, that's almost always a fake — force a real call.
_ACTION_VERBS = re.compile(
    r"\b(opened|opening|launch(?:ed|ing)?|start(?:ed|ing)?|play(?:ed|ing)?|"
    r"set\s+(?:a|the)\s+(?:timer|reminder|alarm)|timer\s+is\s+set|reminder\s+is\s+set|"
    r"sent|sending|search(?:ed|ing)?|created|added|saved|brewing|"
    r"i'?ve\s+(?:set|opened|started|added|saved|created|checked|searched|pulled)|"
    r"let\s+me\s+(?:check|look\s*up|search|pull|grab|open)|pulling\s+up|looking\s+up)\b",
    re.IGNORECASE,
)
# Bare acknowledgments that, in a SHORT reply with no tool actually called, mean the model
# is faking it ("Already done." with nothing opened). Checked only on short replies so real
# conversation isn't caught.
_ACK_RE = re.compile(
    r"\b(already\s+done|all\s+set|consider\s+it\s+done|taken\s+care\s+of|on\s+it|"
    r"right\s+away|done|handled|sorted|good\s+to\s+go|there\s+you\s+go)\b", re.IGNORECASE,
)
_LEGACY_CALL = re.compile(r"<function\s*=\s*([a-zA-Z_]\w*)\s*(\{.*?\})\s*</function>", re.DOTALL)
_TIME_RE = re.compile(r"\d{1,2}:\d{2}|\b[ap]m\b", re.IGNORECASE)
_TIME_TOOLS = {"set_timer", "set_reminder", "snooze_alarm"}

# Tool-gating: the full 48-tool schema is ~9.9k tokens, which blows Groq's free per-request
# limit (forcing every turn to the slower/fewer Cerebras keys). Pure chit-chat doesn't need any
# tool, so we withhold the schema for it — the request shrinks and Groq (9 keys, fastest) serves
# it. This regex is the "might need a tool" detector: it's deliberately INCLUSIVE (a false
# positive just keeps today's behaviour; a false negative is caught by a one-shot retry-with-
# tools in run(), so nothing ever breaks — a missed action just costs one extra round-trip).
_TOOL_HINTS = re.compile(
    r"\b(messages?|msg|texts?|send|sent|dm|dms|whats?app|insta(?:gram)?|e?mail|gmail|"
    r"repl(?:y|ies)|forward|tell\s+(?!me\b|us\b)|ask\s+(?!me\b|us\b)|let\s+\w+\s+know|"
    r"mute|unmute|block|unsend|delete|"
    r"call|calling|dial|redial|decline|answer|silence|hang\s+up|pick\s+up|missed\s+calls?|who\s+called|"
    r"posts?|story|stories|likes?|unlike|likers?|comments?|follow|unfollow|viewers?|followers?|"
    r"inbox|unread|"
    r"remind(?:er)?|timer|alarm|snooze|wake\s+me|"
    r"play|pause|resume|volume|songs?|music|videos?|youtube|screenshot|"
    r"open|launch|close|run|"
    r"search|google|look\s+up|weather|forecast|temperature|news|headlines|"
    r"price|stock|crypto|bitcoin|ethereum|"
    r"remember|recall|notes?|did\s+i|do\s+you\s+remember|"
    r"access|trusted|enroll?|enrol|passphrase|revoke|who\s+has\s+access|remove\s+\w+'?s?\s+access|"
    r"face\s+recogni|re-?verify|verify\s+me|scan\s+my\s+face|who\s+am\s+i|recogni[sz]e\s+me|"
    r"screen|look|looking|see|seeing|watch|camera|webcam|describe|vision|picture|photo|image|read|"
    r"status|battery|cpu|ram|disk|routines?)\b"
    r"|\b(who\s+is|who'?s|what'?s\s+the|what\s+is\s+the|how\s+many|when\s+is|where\s+is|"
    r"what(?:'?s| is)\s+this|what\s+am\s+i\s+(?:holding|showing|looking)|"
    r"current|latest|today'?s|right\s+now)\b",
    re.IGNORECASE,
)


def _might_need_tool(text: str) -> bool:
    """Cheap, inclusive intent check — True unless the message is clearly pure chit-chat."""
    return bool(_TOOL_HINTS.search(text or ""))


def _ensure_timer_time(text: str, trace: list["ToolTrace"]) -> str:
    """Safety net: if a timer/reminder was set but JARVIS forgot to state the clock time
    (e.g. just 'Already done'), append the tool's own confirmation so the user can verify."""
    results = [t.result for t in trace if t.name in _TIME_TOOLS and not t.error]
    if results and not _TIME_RE.search(text or ""):
        tail = results[-1]
        return f"{text} {tail}".strip() if text else tail
    return text

Narrator = Callable[[str], Awaitable[None]]


@dataclass
class ToolTrace:
    name: str
    args: dict
    result: str
    error: str | None = None


@dataclass
class AgentReply:
    text: str
    trace: list[ToolTrace] = field(default_factory=list)
    steps: int = 0
    sleep: bool = False          # user dismissed JARVIS -> go back to wake-word watch
    mood: dict = field(default_factory=dict)   # Phase 5 HUD snapshot {register,warmth,play,...}


_SLEEP_RE = re.compile(r"<\s*sleep\s*>", re.IGNORECASE)


def _extract_sleep(text: str) -> tuple[str, bool]:
    sleep = bool(_SLEEP_RE.search(text or ""))
    return _SLEEP_RE.sub("", text or "").strip(), sleep


def _parse_legacy(blob: str) -> tuple[str, dict] | None:
    m = _LEGACY_CALL.search(blob or "")
    if not m:
        return None
    try:
        args = json.loads(m.group(2))
    except json.JSONDecodeError:
        return None
    return (m.group(1), args) if isinstance(args, dict) else None


def _parse_json_call(text: str) -> tuple[str, dict] | None:
    """Catch a tool call the model emitted as PLAIN-TEXT JSON instead of a real tool_call —
    e.g. {"type":"function","name":"web_search","parameters":{"query":"..."}} or
    {"name":"set_timer","arguments":{...}}. Weaker fallback models do this; without this we'd
    speak the raw JSON aloud instead of running the tool."""
    t = (text or "").strip()
    if "{" not in t or '"name"' not in t and '"function"' not in t:
        return None
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    fn = obj.get("function") if isinstance(obj.get("function"), dict) else obj
    name = fn.get("name")
    args = fn.get("parameters", fn.get("arguments", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, ValueError):
            args = {}
    if isinstance(name, str) and isinstance(args, dict):
        return (name, args)
    return None


# Match a tool-call tag by NAME only — handles <recall>{...}, <recall>{...}</recall>, AND the
# botched <recall{...}> form (JSON glued straight onto the tag name). We gate on a real tool
# name + a JSON body before ever acting/suppressing, so the loose match is safe.
_TAG_OPEN = re.compile(r"<([a-zA-Z_]\w*)", re.DOTALL)


def _extract_json_obj(s: str, start: int = 0) -> dict | None:
    """First balanced {...} object at/after `start`, tolerating trailing junk (extra braces)."""
    i = s.find("{", start)
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                try:
                    o = json.loads(s[i:j + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
                return o if isinstance(o, dict) else None
    return None


def _parse_tag_call(text: str) -> tuple[str, dict] | None:
    """Catch a tool call emitted as an XML-ish tag named after the tool — closed
    (<web_search>{...}</web_search>) OR open/malformed (<web_search>{...}}) since weak models
    botch the closing tag. Grabs the first balanced JSON object after the tag."""
    m = _TAG_OPEN.search(text or "")
    if not m:
        return None
    args = _extract_json_obj(text, m.end())
    return (m.group(1), args) if isinstance(args, dict) else None


def _runnable_call(text: str) -> tuple[str, dict] | None:
    """A tool call hidden in a text reply, but only if it names a REAL registered tool."""
    cand = _parse_json_call(text) or _parse_tag_call(text) or _parse_legacy(text)
    if cand and tools.get(cand[0]) is not None:
        return cand
    return None


def _looks_like_tool_garbage(text: str) -> bool:
    """A reply that is clearly a botched tool call we couldn't run — must never be spoken."""
    t = (text or "").strip()
    if not t:
        return False
    if "<function" in t:
        return True
    m = _TAG_OPEN.search(t)                       # <toolname> for a real tool
    if m and tools.get(m.group(1)) is not None and "{" in t:
        return True
    # a bare JSON object that's really a tool call
    if t.startswith("{") and ('"query"' in t or '"parameters"' in t or '"arguments"' in t
                              or '"function"' in t or '"tool' in t):
        return True
    return False


class AgentRunner:
    def __init__(self) -> None:
        self.rotator = get_rotator()
        tools.discover()

    async def _complete(self, messages: list[dict], **kw) -> dict:
        """chat_complete with a brief retry when every provider is momentarily rate-limited
        (per-minute limits reset in a few seconds, so waiting beats failing)."""
        for attempt in range(3):
            try:
                return await self.rotator.chat_complete(messages, **kw)
            except RuntimeError as e:
                m = str(e).lower()
                transient = any(s in m for s in ("429", "rate", "503", "unavail", "timeout"))
                if transient and attempt < 2:
                    logger.warning("all providers busy — waiting 5s then retrying (%d/2)", attempt + 1)
                    await asyncio.sleep(5)
                    continue
                raise

    def _exec(self, name: str, args: dict, trace: list[ToolTrace], allow=None) -> str:
        t = tools.get(name)
        if t is None:
            trace.append(ToolTrace(name, args, f"[unknown tool: {name}]", error="unknown"))
            return f"[unknown tool: {name}]"
        # Phase 11 backstop: a below-tier speaker's tools are already withheld from the schema,
        # but if a forbidden call still slips through (leaked/forced/hallucinated), refuse it.
        if allow is not None and not allow(t):
            if (t.tier or "").endswith("+passphrase"):
                no_phrase = not identity.get_store().has_passphrase()
                note = ("(This action needs the Owner's security passphrase. No passphrase is set "
                        "yet — tell him to set one first with set_security_passphrase, then re-ask.)"
                        if no_phrase else
                        "(This action needs the Owner's spoken security passphrase, which wasn't "
                        "given. Ask him to authorise it by saying the passphrase — never reveal, "
                        "hint at, or guess the phrase yourself.)")
            else:
                note = ("(Access denied for this speaker — decline politely and in character; do "
                        "NOT explain the restriction, name the tool, or say what it would have done.)")
            trace.append(ToolTrace(name, args, note, error="forbidden"))
            logger.info("tool %s blocked (tier=%s)", name, t.tier)
            return note
        try:
            raw = t.run(args)
            result = raw if len(raw) <= MAX_TOOL_RESULT_CHARS else raw[:MAX_TOOL_RESULT_CHARS] + "\n[truncated]"
            trace.append(ToolTrace(name, args, result))
            logger.info("tool %s(%s) -> %s", name, args, result[:120])
            return result
        except Exception as e:  # noqa: BLE001
            logger.exception("tool %s failed", name)
            trace.append(ToolTrace(name, args, f"[tool error: {e}]", error=str(e)))
            return f"[tool error: {e}]"

    async def run(self, user_text: str, history: list[dict] | None = None,
                  narrate: Narrator | None = None,
                  channel: str = DEFAULT_CHANNEL,
                  voice_emotion: dict | None = None,
                  trust: "identity.Trust | None" = None) -> AgentReply:
        # Tool-gating: withhold the (large) tool schema for pure chit-chat so the request fits
        # Groq's free limit and stays fast. If the model then shows it wanted to act, we flip
        # tools on and retry once (see the not-tool_calls branch below). Clear action requests
        # match the hint regex and get tools immediately — no extra hop.
        gated = not _might_need_tool(user_text)
        # Phase 11 — access control. `trust` is the verified speaker (defaults to Owner/open mode).
        # The schema is filtered to the tools their tier may use, so a below-tier speaker never
        # even sees owner-only tools; `_exec` re-checks as a backstop against leaked/forced calls.
        trust = trust or identity.Trust()
        # `visible` filters the schema by tier (owner still sees passphrase-gated tools so JARVIS
        # can ask for the phrase); `allow` is the execution gate (adds the passphrase requirement).
        visible = (lambda t: identity.tool_visible(t.tier, trust)) if identity.enabled() else None
        allow = (lambda t: identity.tool_allowed(t.tier, trust)) if identity.enabled() else None
        # Only the tools relevant to THIS utterance are sent (core + matches), so the request stays
        # small enough for the fast free providers (sending all 68 was 413-ing Groq). Persona/memory
        # are untouched, so personalization is unchanged.
        schema = None if gated else (tools.for_openai(visible, relevant_to=user_text) or None)
        now = _dt.datetime.now().strftime("%A, %d %B %Y, %I:%M %p").replace(" 0", " ")

        # Phase 5 — read the boss's mood (words + voice tone) and let it drive tone/wit/temperature.
        mood_snapshot: dict = {}
        mood_block = ""
        temperature = 0.4
        if EMOTION_ENABLED:
            try:
                mood_snapshot = await asyncio.to_thread(emotion.analyze, user_text, voice_emotion)
                await asyncio.to_thread(emotion.note_user_turn, user_text)   # count laughter as a hit
                mood_block = emotion.mood_block()
                temperature = emotion.temperature()
                if mood_snapshot:
                    mood_snapshot["prosody"] = emotion.prosody()   # rate/pitch for the TTS voice
                    logger.info("mood: register=%s humor=%.2f temp=%.2f voice=%s",
                                mood_snapshot.get("register"), mood_snapshot.get("humor", 0),
                                temperature, (voice_emotion or {}).get("emotion", "—"))
            except Exception:  # noqa: BLE001
                logger.exception("emotion analyze failed — proceeding neutral")

        # Memory is the OWNER's private context — never surface it to a trusted/guest/unknown
        # speaker (and Phase-11 also blocks writing their turns into it; see the chat router).
        mem_block = ""
        if MEMORY_ENABLED and trust.is_owner:
            try:
                mem_block = await asyncio.to_thread(
                    get_memory().context_block, user_text, channel)
            except Exception:  # noqa: BLE001
                logger.exception("memory context_block failed — proceeding without it")
        speaker_block = identity.prompt_line(trust) if identity.enabled() else ""
        system = build_system_prompt() + (
            (f"\n\n{speaker_block}" if speaker_block else "") +
            (f"\n\n{mem_block}" if mem_block else "") +
            (f"\n\n{mood_block}" if mood_block else "") +
            f"\n\nCurrent local date & time: {now}. Answer time/date questions directly from this."
            "\n\nCONVERSATION CONTROL: You stay in an open conversation. If the user clearly "
            "ends it or dismisses you — e.g. 'goodbye', 'bye', 'that's all', 'go to sleep', "
            "'mute yourself', 'stop listening', 'leave it', 'talk later', 'thanks that's all' "
            "— give a short warm sign-off and append the token <SLEEP> at the very end. NEVER "
            "say the token aloud, never mention it, and never emit it unless the user is truly "
            "done. For normal questions/requests, never emit it."
        )
        messages: list[dict] = [{"role": "system", "content": system}]
        messages += [m for m in (history or []) if m.get("role") in ("user", "assistant")]
        messages.append({"role": "user", "content": user_text})

        trace: list[ToolTrace] = []
        forced_once = False
        force = False
        narrated = False              # narrate only ONCE per turn (no repeated "opening that")
        nonterminal_ran = False       # did any NON-terminal tool run this turn? (gates fast-path)

        for step in range(1, MAX_STEPS + 1):
            try:
                msg = await self._complete(
                    messages, tools=schema,
                    tool_choice="required" if force else "auto",
                    temperature=temperature,
                )
            except ToolUseFailed as e:
                parsed = _parse_legacy(str(e)) or _parse_json_call(str(e))
                if parsed:
                    name, args = parsed
                    if narrate and not narrated:
                        await self._narrate(narrate, name)
                        narrated = True
                    result = self._exec(name, args, trace, allow)
                    cid = f"call_{len(trace)}"
                    messages.append({"role": "assistant", "content": "",
                                     "tool_calls": [{"id": cid, "type": "function",
                                                     "function": {"name": name, "arguments": json.dumps(args)}}]})
                    messages.append({"role": "tool", "tool_call_id": cid, "name": name, "content": result})
                    force = False
                    continue
                # Couldn't get a valid tool call from any model/provider. Retry WITHOUT tools
                # for a plain reply — but if that reply would CLAIM an action (which never
                # actually ran), refuse to fabricate it; tell the truth instead.
                logger.warning("malformed tool call, no recovery — retrying tools-off")
                try:
                    msg2 = await self._complete(messages, tools=None)
                    text2 = (msg2.get("content") or "").strip()
                except Exception:  # noqa: BLE001
                    text2 = ""
                hidden2 = _runnable_call(text2) if step < MAX_STEPS else None
                if hidden2:
                    name, args = hidden2
                    if narrate and not narrated:
                        await self._narrate(narrate, name)
                        narrated = True
                    result = self._exec(name, args, trace, allow)
                    cid = f"call_{len(trace)}"
                    messages.append({"role": "assistant", "content": "",
                                     "tool_calls": [{"id": cid, "type": "function",
                                                     "function": {"name": name, "arguments": json.dumps(args)}}]})
                    messages.append({"role": "tool", "tool_call_id": cid, "name": name, "content": result})
                    force = False
                    continue
                # Refuse to speak a raw JSON blob, a claimed action, or a bare ack ("Right
                # away, sir.") — after a malformed tool call those all mean nothing ran.
                if (not text2 or _ACTION_VERBS.search(text2) or _parse_json_call(text2)
                        or _parse_tag_call(text2) or (len(text2) < 70 and _ACK_RE.search(text2))):
                    return AgentReply(
                        "Apologies, sir — that got garbled on my end and I couldn't carry it "
                        "out. Say it once more?", trace, step)
                return AgentReply(text2, trace, step)
            except Exception as e:  # noqa: BLE001
                logger.exception("agent chat_complete failed")
                # Never read a raw error blob aloud — keep it short and human.
                msg_l = str(e).lower()
                if "429" in msg_l or "rate" in msg_l or "exhaust" in msg_l or "quota" in msg_l:
                    line = ("Every free lane is briefly rate-limited, sir — give it a minute "
                            "and try again.")
                else:
                    line = "I couldn't reach my services just now, sir. One moment and try again."
                return AgentReply(line, trace, step)

            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                text = (msg.get("content") or "").strip()

                # Tool-gating retry: we withheld tools on a chit-chat guess, but the reply shows
                # it wanted to act (leaks a tool call, or claims an action). Enable tools and
                # retry ONCE — the throwaway reply isn't kept, so the model re-answers with tools.
                if gated and (_runnable_call(text) or _ACTION_VERBS.search(text)
                              or (len(text) < 70 and _ACK_RE.search(text))):
                    logger.info("chit-chat guess actually wanted a tool — enabling tools, retrying")
                    schema = tools.for_openai(visible, relevant_to=user_text) or None
                    gated = False
                    continue

                # The model sometimes prints a tool call as plain-text JSON instead of making
                # a real tool_call. Run it for real rather than speaking the raw blob.
                hidden = _runnable_call(text) if step < MAX_STEPS else None
                if hidden:
                    name, args = hidden
                    logger.warning("tool call leaked as text — executing %s instead of speaking it", name)
                    if narrate and not narrated:
                        await self._narrate(narrate, name)
                        narrated = True
                    result = self._exec(name, args, trace, allow)
                    cid = f"call_{len(trace)}"
                    messages.append({"role": "assistant", "content": "",
                                     "tool_calls": [{"id": cid, "type": "function",
                                                     "function": {"name": name, "arguments": json.dumps(args)}}]})
                    messages.append({"role": "tool", "tool_call_id": cid, "name": name, "content": result})
                    force = False
                    continue

                claims_action = _ACTION_VERBS.search(text) or (len(text) < 70 and _ACK_RE.search(text))
                if schema and not trace and not forced_once and claims_action:
                    logger.warning("possible fake action (no tool call): %r — nudging", text[:100])
                    # Nudge, but DON'T force a tool (forcing re-ran tools on "thank you").
                    # If the user really asked for an action, the model will call it; if they
                    # were just chatting/thanking, it replies normally with no tool.
                    messages.append({"role": "system", "content":
                        "Your last reply sounded like you performed an action, but you called "
                        "NO tool. If the user actually asked you to DO something, call the right "
                        "tool now. If they did NOT (just thanking you or chatting), simply reply "
                        "naturally and do NOT call any tool."})
                    forced_once = True
                    continue
                if schema and not trace and forced_once and claims_action:
                    # Already nudged once and it STILL claims an action with no tool call —
                    # almost always a rate-limited fabrication. Refuse to fake it; tell the truth.
                    logger.warning("still claiming action after nudge with no tool — refusing: %r", text[:100])
                    return AgentReply(
                        "Apologies, sir — every free lane is briefly rate-limited, so I couldn't "
                        "carry that out just now. Give it a moment and ask again.", trace, step)
                if _looks_like_tool_garbage(text):
                    logger.warning("reply is botched tool-call garbage — refusing to speak it: %r", text[:120])
                    return AgentReply(
                        "Apologies, sir — that got garbled on my end and I couldn't carry it "
                        "out. Say it once more?", trace, step)
                text, sleep = _extract_sleep(text)
                return self._finalize(
                    AgentReply(_ensure_timer_time(text, trace), trace, step, sleep=sleep),
                    mood_snapshot)

            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})
            step_results: list[str] = []
            step_all_terminal = bool(tool_calls)     # every call this step a clean terminal action?
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if narrate and not narrated:
                    await self._narrate(narrate, name)
                    narrated = True
                result = self._exec(name, args, trace, allow)
                messages.append({"role": "tool", "tool_call_id": tc.get("id", f"call_{len(trace)}"),
                                 "name": name, "content": result})
                t = tools.get(name)
                # A clean terminal result starts with real text (raw "[tool error …]" goes back
                # to the model to phrase). Anything non-terminal disables the fast-path.
                if t and t.terminal and not result.startswith("["):
                    step_results.append(result)
                else:
                    step_all_terminal = False
                    nonterminal_ran = True
            # FAST-PATH: the whole turn was terminal actions whose results are already complete,
            # speakable confirmations (e.g. "WhatsApp sent to Farhan."). Speak them directly and
            # skip the extra round-trip to the LLM — the single biggest latency cut for messaging
            # commands on free providers. (Read/summary tools aren't terminal, so they still get
            # the model's conversational phrasing.)
            if step_all_terminal and not nonterminal_ran and step_results:
                text, sleep = _extract_sleep(" ".join(step_results).strip())
                return self._finalize(AgentReply(text, trace, step, sleep=sleep), mood_snapshot)
            force = False

        last = trace[-1].result if trace else "(nothing)"
        return AgentReply(f"That turned into a longer job than expected. Last step: {last[:200]}", trace, MAX_STEPS)

    @staticmethod
    def _finalize(reply: "AgentReply", mood: dict) -> "AgentReply":
        """Attach the mood snapshot (for the HUD) and remember JARVIS's line so he doesn't recycle
        a quip next turn."""
        reply.mood = mood or {}
        if EMOTION_ENABLED and reply.text:
            try:
                emotion.note_reply(reply.text)
            except Exception:  # noqa: BLE001
                pass
        return reply

    @staticmethod
    async def _narrate(narrate: Narrator, tool_name: str) -> None:
        t = tools.get(tool_name)
        line = (t.narration if t else "") or ""
        if line:
            await narrate(line if line.endswith((".", "…")) else line + ".")


_singleton: AgentRunner | None = None


def get_agent() -> AgentRunner:
    global _singleton
    if _singleton is None:
        _singleton = AgentRunner()
    return _singleton
