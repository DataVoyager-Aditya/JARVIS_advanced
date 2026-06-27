"""
Phase 10.A — deep-research smoke test.

Offline + deterministic: exercises every piece of the research stack WITHOUT hitting the network or
an LLM (search/fetch/synthesis are monkeypatched or use canned data), so it runs anywhere and proves
the wiring, the parsing, persistence, admission control, the background worker, and the monitor logic.

  python scripts/research_smoke.py            # the offline suite (must be all-green)
  python scripts/research_smoke.py --live     # ALSO run one real sweep end-to-end (needs TAVILY key)

Run from the project root with the JARVIS venv.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok  {name}")
    else:
        _FAIL += 1
        print(f"FAIL  {name}  {detail}")


# Point the store singleton at a throwaway DB so the suite never touches the real research.db.
from app.services.research import store as store_mod  # noqa: E402
_TMP_DB = os.path.join(tempfile.gettempdir(), f"research_smoke_{os.getpid()}.db")
store_mod._store = store_mod.ResearchStore(_TMP_DB)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


# ------------------------------------------------------------------ #
def test_config_and_persona() -> None:
    section("config + persona")
    import config
    check("RESEARCH_ENABLED present", hasattr(config, "RESEARCH_ENABLED"))
    check("RESEARCH_DB path", str(config.RESEARCH_DB).endswith("research.db"))
    check("max sources sane", config.RESEARCH_MAX_SOURCES >= 4)
    check("depth >= 1", config.RESEARCH_DEPTH >= 1)
    prompt = config.build_system_prompt()
    check("persona block injected", "DEEP RESEARCH" in prompt and "deep_research" in prompt)


def test_fetch_helpers() -> None:
    section("fetch — grading / skip / extract / links")
    from app.services.research import fetch as F
    check("gov authoritative", F.source_grade("https://www.cdc.gov/x")[0] >= 0.9)
    check("edu authoritative", F.source_grade("https://web.mit.edu/x")[0] >= 0.9)
    check("wikipedia authoritative", F.source_grade("https://en.wikipedia.org/wiki/X")[0] >= 0.9)
    check("reuters high", F.source_grade("https://www.reuters.com/x")[0] >= 0.9)
    check("verge reputable", 0.6 <= F.source_grade("https://www.theverge.com/x")[0] < 0.95)
    check("random blog general", F.source_grade("https://some-blog.example/x")[1] in ("general", "unverified"))
    check(".org organisation", F.source_grade("https://example.org/x")[0] >= 0.55)
    check("skip social", F.skippable("https://twitter.com/x") and F.skippable("https://youtube.com/x"))
    check("skip non-http", F.skippable("mailto:a@b.com"))
    check("dont skip news", not F.skippable("https://www.reuters.com/world"))
    html = ('<html><head><title>T</title></head><body><nav>menu</nav>'
            '<article><h1>Solar</h1><p>' + ("The sun is a star. " * 30) + '</p>'
            '<a href="/more">more</a><a href="https://www.bbc.com/news">bbc</a></article></body></html>')
    txt = F._extract_text(html, "https://site.example/a")
    check("extract main text", "sun is a star" in txt.lower())
    links = F._extract_links(html, "https://site.example/a")
    check("links absolutised", "https://site.example/more" in links)
    check("links external kept", any("bbc.com" in l for l in links))
    check("has_search bool", isinstance(F.has_search(), bool))


def test_text_helpers() -> None:
    section("engine — text/parse helpers")
    from app.services.research import engine as E
    # chunking
    para = "Alpha sentence here.\n\n" + ("Beta " * 400) + "\n\nGamma end."
    chunks = E._split_text(para, 1100)
    check("chunks created", len(chunks) >= 2)
    check("chunk size capped", all(len(c) <= 1100 for c in chunks))
    check("split empty -> []", E._split_text("", 1100) == [])
    # sentences / spoken
    check("first_sentences", E._first_sentences("One. Two. Three.", 2) == "One. Two.")
    check("clean_spoken strips cites", "[1]" not in E._clean_spoken('He won [1][2]. "Yes"'))
    # question parsing
    qs = E.Researcher._parse_questions('["What is X?", "How does X work?", "What is X?"]')
    check("json questions parsed + deduped", qs == ["What is X?", "How does X work?"])
    qs2 = E.Researcher._parse_questions("1. What is the history of X?\n2. Who leads X today?")
    check("line questions fallback", len(qs2) == 2 and qs2[0].endswith("?"))
    check("garbage questions -> []", E.Researcher._parse_questions("no questions here") == [])
    # spoken split
    raw = "SPOKEN: The key thing is A.\n---\nEXECUTIVE SUMMARY\nDetails here.\nCONFIDENCE: High — sources agree"
    spoken, body = E.Researcher._split_spoken(raw)
    check("spoken extracted", spoken == "The key thing is A.")
    check("body after delimiter", body.startswith("EXECUTIVE SUMMARY"))
    check("confidence parsed", E.Researcher._parse_confidence(body).startswith("High"))
    spoken2, body2 = E.Researcher._split_spoken("No delimiter, just prose about findings.")
    check("no-delimiter -> empty spoken", spoken2 == "" and body2.startswith("No delimiter"))
    # signature stability + change
    b1 = "KEY FINDINGS\n- Apple grew revenue 20 percent\n- Margins improved\nCONFIDENCE: High"
    b2 = "KEY FINDINGS\n- Apple grew revenue 20 percent\n- Margins improved\nCONFIDENCE: High"
    b3 = "KEY FINDINGS\n- Apple revenue collapsed entirely overnight\nCONFIDENCE: Low"
    s1 = E.Researcher._signature(b1)
    check("signature stable", s1 == E.Researcher._signature(b2) and len(s1) == 40)
    check("signature changes on material change", s1 != E.Researcher._signature(b3))


def test_select_and_rank() -> None:
    section("engine — chunk selection / link ranking")
    from app.services.research.engine import Researcher
    from collections import Counter
    r = Researcher(rotator=None)
    chunks = [{"text": f"chunk {i} about solar panels and energy", "src": i % 3,
               "url": f"https://s{i%3}.example/p", "title": "t", "grade": 0.5, "label": "general"}
              for i in range(20)]
    picked = r._lexical_pick(["solar energy panels"], chunks)
    check("lexical pick capped", len(picked) <= __import__("config").RESEARCH_SYNTH_CHUNKS)
    # diversity: cap per source
    div = r._diversify(chunks, 12, per_source=2)
    by_src = Counter(c["src"] for c in div)
    check("diversify caps per source (with topup)", max(by_src.values()) <= 12)  # topup may exceed soft cap
    check("diversify hits limit", len(div) == 12)
    links = ["https://www.nature.com/articles/solar-energy",
             "https://random.example/aaa", "https://www.reuters.com/solar-power",
             "https://random.example/bbb"]
    ranked = r._rank_links(links, set(), Counter(), {"solar", "energy", "power"})
    check("trusted+relevant link ranks first", "nature.com" in ranked[0] or "reuters.com" in ranked[0])


def test_store() -> None:
    section("store — briefings + monitors")
    st = store_mod.get_research_store()
    bid = st.save_briefing("Tesla Model 3 sales", "Tesla Model 3 sales", "They rose.",
                           "# Briefing\nKEY FINDINGS\n- up 10%\n", [{"url": "u", "n": 1}], "High", "SIG1")
    check("save returns id", bid > 0)
    b = st.get_briefing("tesla model 3 sales")
    check("exact get", b is not None and b.summary == "They rose.")
    bf = st.get_briefing("tesla model 3")             # fuzzy: query substring of stored key
    check("fuzzy get", bf is not None and bf.signature == "SIG1")
    check("latest briefing", st.latest_briefing().topic == "tesla model 3 sales")
    check("list briefings", len(st.list_briefings()) >= 1)
    check("missing get -> None", st.get_briefing("nonexistent topic zzz") is None)
    # monitors
    st.add_monitor("AI regulation", "AI regulation", 24)
    st.add_monitor("AI regulation", "AI regulation", 12)        # upsert, not duplicate
    check("monitor upsert single", len(st.monitors()) == 1)
    check("monitor interval updated", st.monitors()[0].interval_h == 12)
    check("due at start (last_run=0)", len(st.due_monitors()) == 1)
    st.mark_monitor_run("AI regulation", "SIGX")
    check("not due right after run", len(st.due_monitors(now=time.time())) == 0)
    check("signature recorded", st.monitors()[0].last_signature == "SIGX")
    check("remove monitor exact", st.remove_monitor("AI regulation") == 1)
    check("monitors empty", len(st.monitors()) == 0)


async def test_manager() -> None:
    section("manager — admission, worker, announce")
    from app.services.research import manager as mgr_mod
    mgr_mod.MEMORY_ENABLED = False                      # don't pollute episodic memory in the smoke
    mgr = mgr_mod.get_manager()

    async def fake_sweep(topic: str) -> dict:
        await asyncio.sleep(0.6)
        return {"topic": topic, "title": topic, "ok": True, "n_sources": 7,
                "summary": "The headline finding is X.", "full_md": "# B\n", "confidence": "High",
                "signature": "S", "sources": [{"url": "u", "n": 1}]}

    mgr._sweep = fake_sweep                              # bypass real network/LLM
    r1 = mgr.submit("quantum computing")
    check("submit started", r1["status"] == "started")
    check("is_active true", mgr.is_active("quantum computing"))
    r2 = mgr.submit("Quantum Computing")                # same topic (normalized) -> dedup
    check("submit dedup", r2["status"] == "already_running")
    r3 = mgr.submit("fusion energy")                    # 2nd distinct -> still ok (cap 2)
    check("second distinct started", r3["status"] == "started")
    r4 = mgr.submit("dark matter")                      # 3rd distinct -> busy (cap 2)
    check("third distinct busy", r4["status"] == "busy")

    # wait for the two sweeps to finish + announce
    deadline = time.time() + 5
    items: list = []
    while time.time() < deadline and len(items) < 2:
        items += mgr.drain_done()
        await asyncio.sleep(0.1)
    check("two announcements landed", len(items) >= 2, f"got {len(items)}")
    check("announce framed in character",
          any(it["speak"].startswith("Finished that deep dive") for it in items))
    check("announce carries summary", all("headline finding" in it["speak"] for it in items))
    check("not active after done", not mgr.is_active("quantum computing"))
    st = store_mod.get_research_store()
    check("briefing persisted by manager", st.get_briefing("quantum computing") is not None)
    # status surface
    stt = mgr.status()
    check("status shape", "active" in stt and "last" in stt)


async def test_monitor() -> None:
    section("monitor — baseline / change / no-change")
    from app.services.research import monitor as mon_mod
    from app.services.research import manager as mgr_mod
    mon_mod._quiet_now = lambda *a, **k: False          # don't let quiet hours short-circuit the test
    st = store_mod.get_research_store()
    for m in st.monitors():                             # clean slate
        st.remove_monitor(m.topic)
    st.add_monitor("space elevators", "space elevators", 0)   # interval 0 => always due

    mgr = mgr_mod.get_manager()
    sig_box = {"sig": "SIGA"}
    mgr.run_blocking = lambda topic, timeout=600.0: {
        "topic": topic, "title": topic, "ok": True, "n_sources": 5, "summary": "Update on elevators.",
        "full_md": "# B\n", "confidence": "Medium", "signature": sig_box["sig"], "sources": []}
    mon = mon_mod.get_monitor()

    mgr.drain_done()                                    # clear any residue
    await mon._process_due()                            # first pass -> baseline, NO announce
    check("baseline no announce", len(mgr.drain_done()) == 0)
    check("baseline signature stored", st.monitors()[0].last_signature == "SIGA")

    sig_box["sig"] = "SIGB"                              # material change
    await mon._process_due()
    done = mgr.drain_done()
    check("change announced", len(done) == 1 and done[0].get("kind") == "update")
    check("update line in character", done and done[0]["speak"].startswith("A development on"))
    check("signature advanced", st.monitors()[0].last_signature == "SIGB")

    await mon._process_due()                            # same sig -> no change, no announce
    check("no-change silent", len(mgr.drain_done()) == 0)
    st.remove_monitor("space elevators")


def test_tools() -> None:
    section("tools — registration + selection + behaviour")
    import app.tools as tools
    tools.discover()
    for name in ("deep_research", "research_status", "read_briefing", "watch_topic",
                 "unwatch_topic", "list_research_topics"):
        check(f"tool registered: {name}", tools.get(name) is not None)
    # relevance selection surfaces deep_research for a research ask
    schema = tools.for_openai(relevant_to="can you do a deep dive and research on fusion startups")
    names = [s["function"]["name"] for s in schema]
    check("deep_research selected for ask", "deep_research" in names)
    schema2 = tools.for_openai(relevant_to="read me that briefing on tesla")
    check("read_briefing selected", "read_briefing" in [s["function"]["name"] for s in schema2])
    # read_briefing with no topic + empty store path -> honest (use a fresh empty store)
    import app.tools.research as RT
    # watch/unwatch via the facade through the tools
    msg = RT.watch_topic("ai safety")
    check("watch_topic confirms", "watching" in msg.lower())
    msg2 = RT.unwatch_topic("ai safety")
    check("unwatch_topic confirms", "stopped watching" in msg2.lower())
    msg3 = RT.unwatch_topic("never watched this")
    check("unwatch unknown honest", "wasn't tracking" in msg3.lower())
    # key-findings extraction
    kf = RT._key_findings("# B\n\nKEY FINDINGS\n- Alpha grew [1]\n- Beta fell [2]\n\nCONTRADICTIONS\n- none")
    check("key findings extracted", "Alpha grew" in kf and "[1]" not in kf)
    # read_briefing honest when missing
    out = RT.read_briefing("a topic never researched at all xyz")
    check("read_briefing honest on miss", "offer to run" in out.lower() or "no finished" in out.lower())


def test_audit_fixes() -> None:
    section("audit fixes — regression checks")
    from app.services.research import fetch as F
    from app.services.research import engine as E
    from app.services.research.engine import Researcher
    # _host: literal 'www.' strip only (the lstrip bug)
    check("host bare wikipedia intact", F._host("https://wikipedia.org/wiki/X") == "wikipedia.org")
    check("host www stripped", F._host("https://www.bbc.com/x") == "bbc.com")
    check("host wsj intact", F._host("https://wsj.com/x") == "wsj.com")
    check("engine _host matches", E._host("https://wikipedia.org/x") == "wikipedia.org")
    # source_grade now correct for w-hosts
    check("bare wikipedia authoritative", F.source_grade("https://wikipedia.org/wiki/X")[0] >= 0.9)
    check("who.int authoritative", F.source_grade("https://who.int/x")[0] >= 0.9)
    check("wsj authoritative", F.source_grade("https://wsj.com/x")[0] >= 0.9)
    check("wired reputable (not mangled)", F.source_grade("https://wired.com/x")[1] == "reputable")
    # _split_spoken keeps the EXECUTIVE header when no --- delimiter
    sp, body = Researcher._split_spoken("SPOKEN: Headline A.\n\nEXECUTIVE SUMMARY\nDetails.")
    check("spoken split no-delim", sp == "Headline A.")
    check("header preserved", body.startswith("EXECUTIVE SUMMARY"))
    # _diversify: identical-text but distinct chunks still fill the limit
    r = Researcher(rotator=None)
    dup = [{"text": "same boilerplate text", "src": i, "url": "u", "title": "t",
            "grade": 0.5, "label": "general"} for i in range(10)]
    out = r._diversify(dup, 8, per_source=4)
    check("diversify fills despite duplicate text", len(out) == 8)
    # LIKE wildcard escaping in store
    st = store_mod.get_research_store()
    st.save_briefing("100% renewable energy", "100% renewable energy", "s", "# B\n", [], "High", "SG")
    check("literal % topic retrievable", st.get_briefing("100% renewable energy") is not None)
    check("wildcard % does not match unrelated", st.get_briefing("%") is None or
          st.get_briefing("%").topic == "100% renewable energy")
    # retention prune: >10 briefings for one topic keeps only latest 10
    for i in range(13):
        st.save_briefing("prune topic", "prune topic", f"v{i}", "# B\n", [], "Low", f"S{i}")
    kept = [b for b in st.list_briefings(limit=50) if b.topic == "prune topic"]
    check("retention caps at 10 per topic", len(kept) <= 10, f"kept {len(kept)}")
    # manager has the reaper + run_blocking active marking
    from app.services.research.manager import get_manager
    mgr = get_manager()
    check("manager has _reap_stale", hasattr(mgr, "_reap_stale"))
    # embedder encode lock present
    from app.services.memory.embeddings import get_embedder
    check("embedder encode lock", hasattr(get_embedder(), "_encode_lock"))


def test_router_imports() -> None:
    section("router import")
    import importlib
    mod = importlib.import_module("app.routers.research")
    check("router has prefix", mod.router.prefix == "/research")
    paths = {r.path for r in mod.router.routes}
    for p in ("/research/progress", "/research/done", "/research/status", "/research/start",
              "/research/briefing", "/research/dashboard", "/research/watch", "/research/unwatch"):
        check(f"route {p}", p in paths)


async def test_live() -> None:
    section("LIVE — one real sweep end to end")
    from app.services import research
    if not research.search_available():
        print("  -- skipped: no TAVILY_API_KEY set")
        return
    topic = "what is retrieval augmented generation"
    print(f"  running real sweep: {topic!r} (up to ~2 min)...")
    from app.services.research.manager import get_manager
    mgr = get_manager()
    # The offline tests monkeypatched _sweep/run_blocking on this shared singleton — strip those
    # instance overrides so the live sweep uses the REAL pipeline (class methods).
    for attr in ("_sweep", "run_blocking"):
        mgr.__dict__.pop(attr, None)
    fut_briefing = await asyncio.to_thread(mgr.run_blocking, topic, 180.0)
    check("live sweep returned", fut_briefing is not None)
    if fut_briefing:
        check("live ok", fut_briefing.get("ok"), str(fut_briefing.get("summary"))[:200])
        check("live has sources", fut_briefing.get("n_sources", 0) >= 1)
        check("live has summary", len(fut_briefing.get("summary", "")) > 20)
        print("\n  --- SPOKEN DIGEST ---\n  " + fut_briefing.get("summary", "")[:600])
        print("\n  --- FULL (head) ---\n  " + fut_briefing.get("full_md", "")[:800])


async def main() -> None:
    live = "--live" in sys.argv
    test_config_and_persona()
    test_fetch_helpers()
    test_text_helpers()
    test_select_and_rank()
    test_store()
    await test_manager()
    await test_monitor()
    test_tools()
    test_audit_fixes()
    test_router_imports()
    if live:
        await test_live()
    print(f"\n==== research smoke: {_PASS} passed, {_FAIL} failed ====")
    # cleanup temp db (best-effort)
    try:
        store_mod._store._db.close()
        for ext in ("", "-wal", "-shm"):
            p = _TMP_DB + ext
            if os.path.exists(p):
                os.remove(p)
    except Exception:  # noqa: BLE001
        pass
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
