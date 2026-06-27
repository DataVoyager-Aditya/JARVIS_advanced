"""
Phase 10.F smoke test — Proactive & predictive intelligence.

Verifies the engine WITHOUT a live mic/LLM, AND carries an explicit regression test for every bug
the adversarial audit found (so they can't silently come back): robust time parsing (24h + trailing
qualifier, military HHMM), recurrence qualifiers (weekday/weekend/daily), midnight-wrap routines,
quiet-hours bypass for explicit routines, call-gap using the contact name + answered/outgoing only,
brace-safe idle prompt, record-on-ack (a line counts only once spoken), and the active-user TTL.

Run:  python scripts/proactive_smoke.py    (use the project venv Python)
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "OK " if cond else "XX "
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {mark}{name}" + (f"  -- {detail}" if detail and not cond else ""))


def _hhmm_at(ts: float) -> str:
    t = time.localtime(ts)
    return f"{t.tm_hour:02d}:{t.tm_min:02d}"


def _noon() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0, 0, 0, -1))


def _fresh_engine(tmp: Path, coin_pass: bool = True):
    from app.services.proactive.engine import ProactiveEngine
    from app.services.proactive.store import ProactiveStore
    from app.services.memory.semantic import SemanticStore
    n = f"{time.time_ns()}"
    eng = ProactiveEngine(store=ProactiveStore(tmp / f"p{n}.db"),
                          semantic=SemanticStore(tmp / f"s{n}.db"),
                          rng=types.SimpleNamespace(random=lambda: 0.0 if coin_pass else 0.99))
    eng._owner_active = lambda: True
    eng._register = lambda: "neutral"
    return eng, eng._store, eng._semantic


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    from app.services.proactive.engine import _parse_hhmm, _parse_days, _label
    from app.services.proactive.store import ProactiveStore

    print("\n[1] store: record / caps / dedup / pause (persistent)")
    st = ProactiveStore(tmp / "s1.db")
    check("no fires initially", st.count_today() == 0 and st.last_fire_ts() == 0.0)
    st.record("routine_pre", "pre:gym")
    check("record bumps count + last_fire", st.count_today() == 1 and st.last_fire_ts() > 0)
    check("fired_since sees it", st.fired_since("routine_pre", "pre:gym", 0))
    check("fired_since false for other key", not st.fired_since("routine_pre", "pre:walk", 0))
    st.set_paused_until(time.time() + 3600)
    check("pause roundtrips", st.paused_until() > time.time())
    check("fires persist across reopen", ProactiveStore(tmp / "s1.db").count_today() == 1)

    print("\n[2] time parsing (incl. audit regressions: 24h+suffix, military HHMM)")
    check("18:00", _parse_hhmm("18:00") == (18, 0))
    check("6pm", _parse_hhmm("6pm") == (18, 0))
    check("7:30am", _parse_hhmm("7:30am") == (7, 30))
    check("12am -> 00:00", _parse_hhmm("12am") == (0, 0))
    check("bare hour", _parse_hhmm("6") == (6, 0))
    check("REGRESSION #3: '18:00 daily' parses", _parse_hhmm("18:00 daily") == (18, 0))
    check("REGRESSION #3: '7:00 am on weekdays' parses", _parse_hhmm("7:00 am on weekdays") == (7, 0))
    check("REGRESSION #3: '18:00 sharp' parses", _parse_hhmm("18:00 sharp") == (18, 0))
    check("REGRESSION #4: military '0730'", _parse_hhmm("0730") == (7, 30))
    check("REGRESSION #4: military '1830'", _parse_hhmm("1830") == (18, 30))
    check("rejects junk", _parse_hhmm("whenever") is None)
    check("label strips _time", _label("gym_time") == "gym" and _label("evening_walk") == "evening walk")

    print("\n[2b] recurrence parsing (#10)")
    check("'on weekdays' -> Mon-Fri", _parse_days("7am on weekdays") == {0, 1, 2, 3, 4})
    check("'on weekends' -> Sat/Sun", _parse_days("9am on weekends") == {5, 6})
    check("'daily' -> every day (None)", _parse_days("6pm daily") is None)
    check("bare time -> every day (None)", _parse_days("6pm") is None)
    check("'saturday' -> {5}", _parse_days("10am saturday") == {5})

    print("\n[3] routines from semantic memory (+ .notify opt-out)")
    eng, store, sem = _fresh_engine(tmp)
    sem.set("routines.gym_time", "12:45")
    sem.set("routines.walk_time", "11:20")
    sem.set("routines.nap_time", "13:00")
    sem.set("routines.nap_time.notify", "false")
    names = sorted(n for n, _, _, _ in eng._routines())
    check("reads enabled routines", names == ["gym_time", "walk_time"], f"got {names}")
    check("notify=false routine excluded", "nap_time" not in names)

    print("\n[4] routine triggers + dedup")
    noon = _noon()
    pre = eng._routine_pre(noon)
    check("pre-nudge fires ~45min before", pre and pre["kind"] == "routine_pre" and "gym" in pre["line"])
    check("gym pre-nudge offers the playlist", pre and "playlist" in pre["line"])
    store.record("routine_pre", pre["key"])
    check("pre dedups same day", eng._routine_pre(noon) is None)
    post = eng._routine_post(noon)
    check("post check-in fires after (coin passes)", post and post["kind"] == "routine_post" and "walk" in post["line"])
    eng2, _, sem2 = _fresh_engine(tmp, coin_pass=False)
    sem2.set("routines.walk_time", _hhmm_at(noon - 40 * 60))
    check("post respects the coin (skips)", eng2._routine_post(noon) is None)

    print("\n[4b] REGRESSION #10: weekday qualifier honoured")
    today_wday = time.localtime(noon).tm_wday
    match_q = "on weekdays" if today_wday < 5 else "on weekends"
    miss_q = "on weekends" if today_wday < 5 else "on weekdays"
    ew, _, sew = _fresh_engine(tmp)
    sew.set("routines.gym_time", f"{_hhmm_at(noon + 45 * 60)} {miss_q}")
    check("routine NOT due today (wrong weekday) -> no nudge", ew._routine_pre(noon) is None)
    ew2, _, sew2 = _fresh_engine(tmp)
    sew2.set("routines.gym_time", f"{_hhmm_at(noon + 45 * 60)} {match_q}")
    check("routine due today (right weekday) -> fires", ew2._routine_pre(noon) is not None)

    print("\n[4c] REGRESSION #6: routines wrap around midnight")
    lt = time.localtime()
    now_2350 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 23, 50, 0, 0, 0, -1))
    emw, _, smw = _fresh_engine(tmp)
    smw.set("routines.gym_time", "00:30")                 # tomorrow 00:30 == +40 min from 23:50
    check("pre-nudge fires across midnight", emw._routine_pre(now_2350) is not None)

    print("\n[5] hydration / long-session break")
    eng._active_since = noon - 95 * 60
    hyd = eng._hydration(noon)
    check("hydration fires after ~90min", hyd and "water" in hyd["line"])
    store.record("hydration", "hydration")
    check("hydration dedups within 2h", eng._hydration(noon) is None)
    eng._active_since = noon - 10 * 60
    check("no hydration on a short session", eng._hydration(noon + 200) is None)

    print("\n[6] idle-chatter eligibility + REGRESSION #9 (brace-safe prompt)")
    from config import PROACTIVE_IDLE_MIN_S, PROACTIVE_IDLE_MAX_S
    mid = (PROACTIVE_IDLE_MIN_S + PROACTIVE_IDLE_MAX_S) / 2
    check("eligible in a lull", eng._idle_eligible(noon, {"in_conversation": True, "idle_s": mid}))
    check("not eligible out of conversation", not eng._idle_eligible(noon, {"in_conversation": False, "idle_s": mid}))
    check("not eligible below window", not eng._idle_eligible(noon, {"in_conversation": True, "idle_s": PROACTIVE_IDLE_MIN_S - 5}))
    ebrace, _, sbrace = _fresh_engine(tmp)
    sbrace.set("project.codename", "the {atlas} dashboard with {json} bits")   # braces in the value
    try:
        p = ebrace._idle_prompt()
        check("idle prompt is brace-safe (no .format crash)", "Aditya" in p or "sir" in p.lower() or len(p) > 0)
    except Exception as e:  # noqa: BLE001
        check("idle prompt is brace-safe (no .format crash)", False, str(e))

    print("\n[7] gate: hard gate (paused/cap/min-gap) + quiet hours")
    check("quiet 2am blocks", eng._quiet_hours(time.mktime(time.strptime("2026-06-23 02:00", "%Y-%m-%d %H:%M"))))
    check("quiet noon allows", not eng._quiet_hours(noon))
    eg, sg, _ = _fresh_engine(tmp)
    sg.set_paused_until(time.time() + 600)
    check("paused blocks hard gate", not eg._hard_ok(time.time()))
    eg.resume()
    check("resume re-opens hard gate", eg._hard_ok(time.time()))
    ecap, scap, _ = _fresh_engine(tmp)
    from config import PROACTIVE_DAILY_CAP
    for _ in range(PROACTIVE_DAILY_CAP):
        scap.record("idle", "x")
    check("daily cap blocks", not ecap._hard_ok(time.time()))
    egap, sgap, _ = _fresh_engine(tmp)
    sgap.record("idle", "x")
    check("min-gap blocks back-to-back", not egap._hard_ok(time.time()))

    print("\n[8] REGRESSION #5: routine pre-nudge BYPASSES quiet hours; others don't")
    eqb, sqb, semqb = _fresh_engine(tmp)
    eqb._quiet_hours = lambda now: True                  # pretend it's quiet hours
    semqb.set("routines.gym_time", _hhmm_at(time.time() + 45 * 60))
    rqb = asyncio.run(eqb.poll({"in_conversation": False, "idle_s": 5}))
    check("routine pre-nudge fires even in quiet hours", rqb["kind"] == "routine_pre")
    eqb2, _, semqb2 = _fresh_engine(tmp)
    eqb2._quiet_hours = lambda now: True
    semqb2.set("routines.gym_time", _hhmm_at(time.time() - 40 * 60))   # only a POST candidate
    eqb2._idle_eligible = lambda now, s: True
    # call_gap/hydration/idle are quiet-suppressed; with only a post candidate + quiet, post still
    # fires (explicit routine), but nothing unsolicited does. Just assert it didn't crash + shape.
    rqb2 = asyncio.run(eqb2.poll({"in_conversation": True, "idle_s": 200}))
    check("quiet-hours poll returns documented shape", set(rqb2) == {"say", "kind", "key", "expects_reply"})

    print("\n[9] poll() + record-on-ack (#11): a line counts only once acked")
    epoll, spoll, sempoll = _fresh_engine(tmp)
    sempoll.set("routines.gym_time", _hhmm_at(time.time() + 45 * 60))
    res = asyncio.run(epoll.poll({"in_conversation": False, "idle_s": 5}))
    check("poll returns shape incl. key", set(res) == {"say", "kind", "key", "expects_reply"} and res["key"])
    check("poll fires routine_pre", res["kind"] == "routine_pre" and res["expects_reply"] is True)
    check("poll did NOT record yet (record-on-ack)", spoll.count_today() == 0)
    check("poll is idempotent before ack (still returns the line)",
          asyncio.run(epoll.poll({"idle_s": 5}))["kind"] == "routine_pre")
    epoll.record(res["kind"], res["key"])               # the ack
    check("after ack it counts", spoll.count_today() == 1)
    check("after ack it dedups (silent)", asyncio.run(epoll.poll({"idle_s": 5}))["say"] is None)
    epoll.pause(60)
    check("paused poll stays silent", asyncio.run(epoll.poll({"idle_s": 5}))["say"] is None)

    print("\n[10] REGRESSION #1/#2/#12: call-gap uses name + answered/outgoing only")
    import app.services.calls.store as cstore
    from app.services.calls.store import ANSWERED, MISSED
    cstore._store = cstore.CallStore(tmp / "calls.db")
    now = time.time()
    cstore._store.add(ANSWERED, number="+919871521319", name="Sandhya", ts=now - 20 * 86400, ref="a1")
    cstore._store.add(MISSED, number="+919871521319", name="Sandhya", ts=now - 1 * 86400, ref="m1")
    ecall, _, _ = _fresh_engine(tmp)
    import app.services.messaging.contacts as _ct
    if _ct.display("+919871521319", "call") == "Mom":
        gap = ecall._call_gap(now)
        check("call-gap uses the relationship name (Mom), not the number",
              gap and "Mom" in gap["line"] and "Sandhya" not in gap["line"])
        check("call-gap ignores the recent MISSED call (uses 20d-old ANSWERED)",
              gap and "20 days" in gap["line"])
    else:
        print("     (MY_CONTACTS has no Mom mapping — skipping the relationship-name assertion)")
    cstore._store = cstore.CallStore(tmp / "calls2.db")
    cstore._store.add(ANSWERED, number="+10000000001", name="Aman", ts=now - 25 * 86400, ref="b1")
    ecall2, _, _ = _fresh_engine(tmp)
    gap2 = ecall2._call_gap(now)
    check("call-gap falls back to the phone name when no [call] rule", gap2 and "Aman" in gap2["line"])

    print("\n[11] tools + router")
    from app.tools import discover, get
    discover()
    for tname in ("set_routine", "proactive_control"):
        t = get(tname)
        check(f"tool '{tname}' (owner, terminal)", t is not None and t.tier == "owner" and t.terminal)
    from app.tools.proactive import _routine_slug
    check("routine slug normalises", _routine_slug("gym") == "gym" and _routine_slug("morning walk time") == "morning_walk")
    import app.services.proactive.engine as eng_mod
    tctrl, sctrl, _ = _fresh_engine(tmp)
    eng_mod._engine = tctrl
    msg = get("proactive_control").run({"action": "pause", "minutes": 30})
    check("proactive_control pause persists", "30 minutes" in msg and sctrl.paused_until() > time.time())
    get("proactive_control").run({"action": "resume"})
    check("proactive_control resume clears", sctrl.paused_until() <= time.time())
    from app.main import app
    paths = {r.path for r in app.routes}
    check("/proactive/poll + /ack + /status mounted",
          {"/proactive/poll", "/proactive/ack", "/proactive/status"} <= paths)

    print("\n[12] REGRESSION #13: active-user TTL (a stale guest reverts to Owner)")
    from app.services import identity
    from app.services.identity.trust import Trust
    import app.services.identity.trust as trustmod
    identity.set_active(Trust(tier="guest", name="Guest", display="Guest", source="voice", confidence=0.9))
    check("a guest turn sets the active user", identity.get_active().tier == "guest")
    trustmod._active_ts = time.time() - 99999          # make it stale
    check("a STALE non-owner reverts to Owner (proactive not muted)", identity.get_active().is_owner)
    identity.set_active(Trust(tier="owner", name="owner", display="Aditya", source="device"))  # restore

    print("\n[12b] listener wiring (static) — #7 crash-safety, #8 defer, #11 ack")
    lsrc = (ROOT / "scripts" / "jarvis_listener.py").read_text(encoding="utf-8")
    check("REGRESSION #8: defers non-idle nudges mid-conversation",
          'self._in_conversation and kind != "idle"' in lsrc)
    check("REGRESSION #11: acks after speaking (ACK_URL)", "ACK_URL" in lsrc and "/proactive/ack" in lsrc)
    check("REGRESSION #7: loop body guarded so one TTS error can't kill it",
          "proactive loop iteration failed" in lsrc)

    print("\n[13] persona block")
    from config import build_system_prompt
    sp = build_system_prompt()
    check("PROACTIVE persona block present", "SPEAKING UP ON YOUR OWN" in sp)
    check("persona has the <SILENT> escape hatch", "<SILENT>" in sp)

    print(f"\n==== proactive (10.F) smoke: {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
