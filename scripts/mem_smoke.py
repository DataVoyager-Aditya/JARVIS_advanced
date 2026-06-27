"""
Phase 4 memory smoke test — runs the whole layer end to end in an ISOLATED temp store
(your real memory.db is never touched). Verifies:

  1. episodic add + semantic vector recall (FAISS)
  2. semantic facts set/get + prompt injection
  3. knowledge graph triples + describe
  4. remember_fact / recall tool paths
  5. context_block (passive recall injected into the prompt)
  6. PERSISTENCE across a simulated restart (new instances, same files)
  7. recall/remember registered as agent tools
  8. nightly consolidator on a real free LLM call (summary + facts + triples)
  9. AgentRunner injects the memory block into its system prompt

Run:  python scripts/mem_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK, FAIL = "  [OK] ", "  [FAIL] "
_fails = 0


def check(cond: bool, msg: str) -> None:
    global _fails
    print((OK if cond else FAIL) + msg)
    if not cond:
        _fails += 1


def banner(t: str) -> None:
    print(f"\n=== {t} ===")


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_mem_"))
    print(f"isolated store: {tmp}")

    # Point the memory package at the temp store BEFORE building any service.
    import app.services.memory as mempkg
    mempkg.EPISODIC_DB = tmp / "episodic.db"
    mempkg.EPISODIC_INDEX = tmp / "episodic.faiss"
    mempkg.MEMORY_DB = tmp / "memory.db"

    from app.services.memory import MemoryService

    banner("1-3. Build + tiers")
    mem = MemoryService()
    check(mem.stats()["embeddings"], "local embedding model available")

    # Tier 2 episodic
    mem.episodic.add("Aditya is building an AI assistant called JARVIS in Python.",
                     kind="turn", role="user")
    mem.episodic.add("We discussed switching the database to PostgreSQL next week.",
                     kind="turn", role="user")
    mem.episodic.add("The weather in Bangalore was pleasant today.", kind="turn", role="assistant")
    hits = mem.episodic.search("what project am I working on", k=3)
    check(any("JARVIS" in h.text for h in hits), "vector recall finds the JARVIS project turn")
    check(hits[0].text != "" and 0.0 <= hits[0].score <= 1.0001, "recall returns a sane score")

    # Tier 3 semantic
    mem.semantic.set("user.full_name", "Aditya Raj Thakur")
    mem.semantic.set("prefs.coffee_order", "black, no sugar")
    check(mem.semantic.get("user.full_name") == "Aditya Raj Thakur", "semantic get round-trips")
    lines = mem.semantic.as_prompt_lines()
    check(any("coffee" in l for l in lines), "semantic facts render as prompt lines")

    # Graph
    mem.graph.add_triple("aditya", "works_on", "project_jarvis")
    mem.graph.add_triple("vikram", "is", "brother")
    check("project_jarvis" in mem.graph.describe("aditya"), "graph describes aditya's relations")
    check(mem.graph.counts()[1] == 2, "graph stored 2 relations")

    banner("4. remember_fact / recall tool paths")
    mem.remember_fact("Aditya's brother Vikram lives in Pune.", key="contacts.vikram.location")
    r = mem.recall("Vikram")
    check("pune" in r.lower() or "brother" in r.lower(), f"recall('Vikram') surfaces him")
    check(mem.semantic.get("contacts.vikram.location") == "Aditya's brother Vikram lives in Pune.",
          "remember_fact with key also wrote a semantic fact")

    banner("5. context_block (passive recall)")
    ctx = mem.context_block("remind me about my coffee preference")
    check("WHAT YOU KNOW ABOUT HIM" in ctx, "context block includes durable facts")
    check("coffee" in ctx.lower(), "context block surfaces the coffee fact")

    banner("6. PERSISTENCE across a simulated restart")
    ep_count, fact_count = mem.episodic.count(), mem.semantic.count()
    del mem
    mem2 = MemoryService()  # fresh instances, same files on disk
    check(mem2.episodic.count() == ep_count, f"episodes survived restart ({ep_count})")
    check(mem2.semantic.get("prefs.coffee_order") == "black, no sugar", "facts survived restart")
    hits2 = mem2.episodic.search("what project am I working on", k=3)
    check(any("JARVIS" in h.text for h in hits2), "FAISS index reloaded — vector recall still works")

    banner("7. agent tools registered")
    import app.tools as tools
    tools.discover()
    names = {t.name for t in tools.all_tools()}
    check("remember" in names and "recall" in names, "remember + recall are live tools")

    banner("8. nightly consolidator (real free LLM call)")
    # seed a short 'day' of turns, then distill
    for t in [
        ("user", "By the way, my sister's name is Meera and she's a doctor in Delhi."),
        ("assistant", "Noted, sir."),
        ("user", "I usually go to the gym at 7am on weekdays."),
        ("assistant", "Understood."),
    ]:
        mem2.episodic.add(t[1], kind="turn", role=t[0])
    try:
        res = await mem2.consolidate_now(hours=24.0)
        print(f"    consolidation result: {res}")
        check(res.get("summary") is not None, "consolidator produced a day summary")
        check(res.get("facts", 0) >= 1 or res.get("triples", 0) >= 1,
              "consolidator extracted at least one fact or triple")
    except Exception as e:  # noqa: BLE001
        check(False, f"consolidator raised: {e}")

    banner("9. AgentRunner injects memory into its system prompt")
    mempkg._memory = mem2  # make get_memory() return our temp store
    from app.services.agent.runner import AgentRunner
    runner = AgentRunner()
    # Monkeypatch the rotator so we can capture the system prompt without a real chat call.
    captured = {}

    async def fake_complete(messages, **kw):
        captured["system"] = messages[0]["content"]
        return {"content": "It's a flat white, sir.", "tool_calls": []}

    runner._complete = fake_complete  # type: ignore
    reply = await runner.run("what's my coffee order again?")
    check("WHAT YOU KNOW ABOUT HIM" in captured.get("system", ""),
          "runner injected durable facts into the system prompt")
    check("MEMORY (CRITICAL" in captured.get("system", ""), "memory persona block present")

    print("\n" + ("ALL MEMORY CHECKS PASSED" if _fails == 0 else f"{_fails} CHECK(S) FAILED"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
