"""
Phase 5 — Emotion / humor / personalization smoke test.

  - register detection across the 7 states (LIVE local model)
  - mood state: EMA smoothing, humor budget per register, prompt block, recent-banter dedup
  - voice-tone fusion (a flat 'i'm fine' that SOUNDS angry -> frustrated)
  - prosody per register, humor-hit detection
  - end-to-end: the agent attaches a mood snapshot to its reply

Run:  python scripts/emotion_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ok = 0
fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print(f"  OK {name}")
    else:
        fail += 1; print(f"  XX {name}  {extra}")


def main() -> int:
    import app.services.emotion as emo
    from app.services.emotion.detector import get_detector
    from app.services.emotion.state import get_mood

    print("[1] register detection (live local model)")
    cases = [("ugh this stupid bug again, fml", "frustrated"),
             ("hahaha that's gold lmao", "playful"),
             ("i'm so exhausted, i feel like giving up", "vulnerable"),
             ("send it right now, asap", "urgent"),
             ("oh great, just what i needed today", "sarcastic"),
             ("i just shipped the whole thing, nailed it", "showing_off"),
             ("what's on my calendar", "neutral")]
    det = get_detector()
    hits = 0
    for text, want in cases:
        r = det.read(text)
        hit = r.register == want
        hits += hit
        check(f"{want:12} <- {text[:34]!r}", hit, f"got {r.register}")
    check("register accuracy >= 6/7", hits >= 6, f"{hits}/7")

    print("[2] mood state: humor budget tracks register")
    get_mood().update(det.read("lol you're hilarious"))
    hi = get_mood().snapshot()["humor"]
    get_mood().update(det.read("i need this fixed right now asap"))
    lo = get_mood().snapshot()["humor"]
    check("humor high on playful, ~0 on urgent", hi > 0.4 and lo < 0.2, f"{hi} -> {lo}")

    print("[3] prompt block + recent-banter dedup")
    get_mood().note_reply("Third 'last coffee' today, sir.")
    block = emo.mood_block()
    check("block has register + humor budget", "register" in block.lower() and "humor budget" in block.lower())
    check("recent line fed back (no repeats)", "last coffee" in block.lower())

    print("[4] prosody per register")
    get_mood().update(det.read("i'm really down today"))
    pv = emo.prosody()
    get_mood().update(det.read("haha nice one"))
    pp = emo.prosody()
    check("vulnerable slows voice, playful lifts it", "-" in pv["rate"] and "+" in pp["rate"], f"{pv} / {pp}")

    print("[5] voice-tone fusion (tone overrides flat words)")
    snap = emo.analyze("i'm fine", voice_emotion={"emotion": "angry", "intensity": 0.8})
    check("flat 'i'm fine' said angrily -> frustrated", snap["register"] == "frustrated", snap["register"])

    print("[6] humor-hit detection")
    before = get_mood().humor_hits
    emo.note_user_turn("lmao that's so funny")
    check("laughter counts as a humor hit", get_mood().humor_hits == before + 1)

    print("[7] end-to-end: agent attaches mood to reply")
    from app.services.agent.runner import get_agent
    try:
        rep = asyncio.run(get_agent().run("ugh everything is breaking today, i'm so done",
                                          history=[], narrate=None))
        check("reply carries a mood snapshot", bool(rep.mood) and "register" in rep.mood, str(rep.mood)[:80])
        print(f"     register={rep.mood.get('register')} | reply: {rep.text[:90]}")
    except Exception as e:  # noqa: BLE001
        check("agent end-to-end", False, f"{type(e).__name__}: {e}")

    print(f"\n==== emotion smoke: {ok} passed, {fail} failed ====")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
