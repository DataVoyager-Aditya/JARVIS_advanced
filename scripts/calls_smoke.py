"""
Phase 8 smoke test — Calls (Android companion bridge).

Verifies everything checkable WITHOUT a paired phone (the companion + live ring need Aditya's
one-time setup): the persistent call log + dedup, the live-ring command queue with its TTL, the
spoken-announcement buffer, missed-call surfacing, the auto-handle rules (Phase 8.5), every
call tool's in-character behaviour and schema, the router mount + token gating, and the persona
block. Run:  python scripts/calls_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "OK " if cond else "XX "
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {mark}{name}" + (f"  — {detail}" if detail and not cond else ""))


def main() -> None:
    # Isolate the call log in a temp DB so the smoke never pollutes the real one.
    import app.services.calls.store as store_mod
    tmp = Path(tempfile.mkdtemp()) / "calls_test.db"
    store_mod._store = store_mod.CallStore(tmp)
    store = store_mod.get_call_store()

    from app.services import calls
    from app.services.calls.store import MISSED, INCOMING

    print("\n[1] store: insert / dedup / recent / missed / persistence")
    a = store.add(INCOMING, number="+15550001", name="Mom", ref="c:1")
    b = store.add(MISSED, number="+15550002", name="Vikram", ref="c:2")
    dup = store.add(MISSED, number="+15550002", name="Vikram", ref="c:2")  # same ref -> ignored
    check("inserts return ids", bool(a) and bool(b))
    check("duplicate ref ignored", dup is None, f"dup={dup}")
    check("count == 2", store.count() == 2, f"count={store.count()}")
    check("missed() finds the missed call", len(store.missed()) == 1)
    check("recent() newest-first", store.recent()[0].ref == "c:2")
    reopened = store_mod.CallStore(tmp)
    check("persists across reopen", reopened.count() == 2)

    print("\n[2] store: mark_seen + rules")
    check("missed unseen before", store.missed_count(only_unseen=True) == 1)
    store.mark_seen(kind=MISSED)
    check("missed seen after mark", store.missed_count(only_unseen=True) == 0)
    store.set_rule("Mom", "auto_text", "call you back, mum")
    store.set_rule("spam", "auto_decline")
    check("rules stored", len(store.rules()) == 2)
    check("rule upsert (no dup)", (store.set_rule("Mom", "auto_answer"), len(store.rules()))[1] == 2)
    check("clear_rule works", store.clear_rule("spam") and len(store.rules()) == 1)

    print("\n[3] service: live ring -> command queue (+ TTL)")
    calls._pending = None
    calls._commands.clear()
    calls._spoken.clear()
    res = asyncio.run(calls.record_incoming(number="+15550009", name="Farhan", ref="ring:1"))
    check("record_incoming returns call_id", bool(res.get("call_id")))
    check("pending() is the live ring", (calls.pending() or {}).get("name") == "Farhan")
    drained = calls.drain_spoken()
    check("incoming line buffered with kind",
          any("Farhan" in it["line"] and it["kind"] == "incoming" for it in drained))
    q = calls.queue_command("reject")               # synonym for decline
    check("decline queued while live", q["ok"] and q["live"] and q["action"] == "decline")
    cmds = calls.take_commands()
    check("companion pulls the command", len(cmds) == 1 and cmds[0]["action"] == "decline")
    check("take_commands clears the queue", calls.take_commands() == [])
    check("pending cleared after decline", calls.pending() is None)
    q2 = calls.queue_command("decline")
    check("no command when nothing rings", (not q2["ok"]) and (not q2["live"]))

    # TTL: a ring older than CALL_RING_TTL_S is no longer commandable.
    asyncio.run(calls.record_incoming(number="+1", name="Stale", ref="ring:2"))
    calls._pending["ts"] = time.time() - 9999
    check("stale ring not pending", calls.pending() is None)
    check("stale ring rejects commands", calls.queue_command("answer")["live"] is False)

    print("\n[4] service: missed clears ring, sync_log, normalize, set_rule")
    calls._pending = None
    asyncio.run(calls.record_incoming(number="+15550011", name="Riya", ref="ring:3"))
    asyncio.run(calls.record_missed(number="+15550011", name="Riya", ref="miss:3"))
    check("a missed call clears the live ring", calls.pending() is None)
    n = calls.sync_log([{"kind": "missed", "number": "+1", "name": "A", "ref": "log:1"},
                        {"kind": "incoming", "number": "+2", "name": "B", "ref": "log:2"},
                        {"kind": "missed", "number": "+1", "name": "A", "ref": "log:1"}])  # dup
    check("sync_log upserts only new", n == 2, f"new={n}")
    check("normalize_action maps phrases",
          calls.normalize_action("pick up") == "answer"
          and calls.normalize_action("hang up") == "decline"
          and calls.normalize_action("mute it") == "silence")
    sr = calls.set_rule("Mom", "auto_text", "in a meeting, call you back")
    check("set_rule (auto_text) ok", sr["ok"] and any(r["match"] == "mom" for r in calls.get_rules()))
    check("set_rule off removes it", calls.set_rule("Mom", "off")["ok"]
          and not any(r["match"] == "mom" for r in calls.get_rules()))

    print("\n[4b] service: outbound dialing queue (Phase 8.5)")
    calls._commands.clear()
    d = calls.queue_dial("9871521319")
    check("queue_dial accepts a number (no live ring needed)", d["ok"] and d["number"] == "9871521319")
    check("queue_dial rejects empty", not calls.queue_dial("")["ok"])
    dcmds = calls.take_commands()
    check("companion pulls the dial command",
          any(c["action"] == "dial" and c["number"] == "9871521319" for c in dcmds))

    print("\n[5] tools: registration + in-character behaviour")
    from app.tools import discover, get
    discover()
    for tname in ("phone_call_action", "set_call_rule", "list_call_rules", "clear_call_log"):
        t = get(tname)
        check(f"tool '{tname}' registered", t is not None)
    pca = get("phone_call_action")
    check("phone_call_action is NON-terminal (model phrases it naturally)", not pca.terminal)
    # read_missed reads the log; decline with nothing ringing degrades honestly
    calls._pending = None
    out_missed = pca.run({"action": "read_missed"})
    check("read_missed returns a summary or 'caught up'", isinstance(out_missed, str) and len(out_missed) > 0)
    # missed calls are named by his relationship word (number -> Mom), not the phone's raw name
    store.add(MISSED, number="+919871521319", name="Sandhya", ref="miss:name")
    import app.services.messaging.contacts as _ct
    if _ct.display("+919871521319", "call") == "Mom":     # only if MY_CONTACTS maps it
        check("missed call shows his name for the caller (Mom, not Sandhya)",
              "Mom" in pca.run({"action": "read_missed"}) and "Sandhya" not in pca.run({"action": "read_missed"}))
    # clear tool
    clr = get("clear_call_log")
    check("clear_call_log wipes the log", "Cleared" in clr.run({"scope": "all"}) or "already clear" in clr.run({"scope": "all"}))
    check("clear_call_log empties it", store.count() == 0)
    out_recent = pca.run({"action": "recent"})
    check("recent returns the log", "Recent calls" in out_recent or "No calls" in out_recent)
    out_decline = pca.run({"action": "decline"})
    check("decline w/ no ring is honest", "no call ringing" in out_decline.lower())
    out_cb = pca.run({"action": "callback"})
    check("callback redirects to place_call", "place_call" in out_cb.lower())
    out_rule = get("set_call_rule").run({"contact": "Boss", "action": "auto_answer"})
    check("set_call_rule confirms", "auto-answer" in out_rule.lower())
    out_list = get("list_call_rules").run({})
    check("list_call_rules shows it", "Boss".lower() in out_list.lower())
    # dialing tool
    pc2 = get("place_call")
    check("place_call registered + terminal", pc2 is not None and pc2.terminal)
    check("place_call dials a raw number", "calling" in pc2.run({"contact": "+919876543210"}).lower())
    check("place_call honest when no number", "don't have a number"
          in pc2.run({"contact": "Zzznobodyxyz"}).lower())

    print("\n[6] router: mount + token gating")
    from app.main import app
    paths = {r.path for r in app.routes}
    for p in ("/calls/incoming", "/calls/missed", "/calls/ended", "/calls/sync",
              "/calls/commands", "/calls/command", "/calls/recent", "/calls/announcements",
              "/calls/rules"):
        check(f"route {p} mounted", p in paths)
    from fastapi.testclient import TestClient
    client = TestClient(app)
    r_bad = client.post("/calls/incoming", json={"number": "+1", "name": "X"})
    check("incoming rejects a missing/bad token (401)", r_bad.status_code == 401, f"got {r_bad.status_code}")
    from config import CALLS_WEBHOOK_TOKEN
    r_ok = client.post("/calls/incoming", json={"number": "+1", "name": "X"},
                       headers={"x-jarvis-token": CALLS_WEBHOOK_TOKEN})
    check("incoming accepts the right token", r_ok.status_code == 200 and r_ok.json().get("ok"))
    r_cmd = client.post("/calls/command", json={"action": "decline"})
    check("POST /calls/command is open to the PWA", r_cmd.status_code == 200)
    r_rules = client.get("/calls/rules", headers={"x-jarvis-token": CALLS_WEBHOOK_TOKEN})
    check("companion can pull rules with token", r_rules.status_code == 200 and "rules" in r_rules.json())

    print("\n[7] persona: calls block present")
    from config import build_system_prompt
    sp = build_system_prompt()
    check("CALLS persona block in system prompt", "CALLS (his phone" in sp)
    check("persona forbids fabricating calls", "never pretend" in sp.lower() or "didn't action" in sp.lower())

    print(f"\n==== calls smoke: {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
