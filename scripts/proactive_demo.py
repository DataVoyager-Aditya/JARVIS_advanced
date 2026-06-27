"""
Phase 10.F demo - see the ACTUAL proactive lines JARVIS would speak, instantly.

Runs the real engine on throwaway temp stores (touches nothing of yours), feeds it a few
situations, and prints the line it would say. No backend, no mic, no waiting.

Run:  python scripts/proactive_demo.py
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tempfile


def main() -> None:
    from app.services.proactive.engine import ProactiveEngine
    from app.services.proactive.store import ProactiveStore
    from app.services.memory.semantic import SemanticStore

    tmp = Path(tempfile.mkdtemp())
    eng = ProactiveEngine(store=ProactiveStore(tmp / "p.db"),
                          semantic=SemanticStore(tmp / "s.db"),
                          rng=types.SimpleNamespace(random=lambda: 0.0))  # coin always passes
    sem = eng._semantic

    lt = time.localtime()
    noon = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0, 0, 0, -1))

    def at(mins_from_noon: int) -> str:
        t = time.localtime(noon + mins_from_noon * 60)
        return f"{t.tm_hour:02d}:{t.tm_min:02d}"

    print("\nProactive demo - example lines JARVIS would speak:\n" + "-" * 58)

    # Routine pre-nudge: gym 45 min ahead.
    sem.set("routines.gym_time", at(45))
    pre = eng._routine_pre(noon)
    print(f"\n[routine pre-nudge] (gym block at {at(45)}, it's 12:00)\n   -> {pre['line'] if pre else '(none)'}")

    # Routine check-in: walk 40 min ago.
    sem.set("routines.walk_time", at(-40))
    post = eng._routine_post(noon)
    print(f"\n[routine check-in]  (walk was at {at(-40)})\n   -> {post['line'] if post else '(none)'}")

    # Hydration: 95 minutes heads-down.
    eng._active_since = noon - 95 * 60
    hyd = eng._hydration(noon)
    print(f"\n[hydration / break] (95 min heads-down)\n   -> {hyd['line'] if hyd else '(none)'}")

    # Call gap (only if a real, named caller is >14 days stale in YOUR call log - usually none here).
    gap = eng._call_gap(time.time())
    print(f"\n[call gap]          (from your real call log)\n   -> {gap['line'] if gap else '(nothing stale - needs a >14-day-old named call)'}")

    # Idle chatter is LLM-composed at runtime (a quiet lull mid-conversation) - shown live, not here.
    print("\n[idle chatter]      composed live by the model in a conversation lull "
          "(or it stays <SILENT>).")
    print("-" * 58)
    print("These fire for real once you restart JARVIS - see the script header / STATUS for the gate.\n")


if __name__ == "__main__":
    main()
