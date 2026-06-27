"""
Mobile-UI smoke test (Phase 9 — phone shell).

Verifies the phone UI built from `JARVIS-Mobile.html` WITHOUT a real device:

  1. build_pwa builds app/web/mobile.html and its own dc-runtime-mobile.js, and the desktop
     shell (index.html) is byte-for-byte UNCHANGED by the mobile build (the hard constraint:
     "on PC nothing changes — only mobile").
  2. The mobile component is real backend wiring, not the mockup: /chat, /voice/stt,
     /voice/tts/stream, /events/ws, /messaging/inbox, /memory/graph, /calls/command.
  3. The in-app status bar (clock + battery + signal) is stripped; persona is JARVIS (no Friday).
  4. The patched dc-script parses as valid JS (node --check).
  5. web.py serves mobile.html to phones (User-Agent) and the IDENTICAL index.html to desktop,
     with ?ui= overrides.

Run:  python scripts/mobile_smoke.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'OK ' if cond else 'XX '}{name}" + (f"  — {detail}" if detail and not cond else ""))


def main() -> None:
    WEB = ROOT / "app" / "web"
    index_path = WEB / "index.html"
    mobile_path = WEB / "mobile.html"

    print("\n[1] build: mobile.html emitted; PC shell untouched")
    before = index_path.read_bytes() if index_path.exists() else None
    import scripts.build_pwa as bp
    bp.main()                                   # full build (PC + mobile)
    check("index.html exists", index_path.exists())
    check("mobile.html exists", mobile_path.exists())
    check("dc-runtime-mobile.js exists", (WEB / "static" / "dc-runtime-mobile.js").exists())
    after = index_path.read_bytes()
    check("PC index.html unchanged by rebuild", before is None or before == after,
          "desktop shell changed!")
    check("mobile.html != index.html", mobile_path.read_bytes() != after)

    m = mobile_path.read_text(encoding="utf-8")

    print("\n[2] status bar stripped + JARVIS persona")
    check("no in-app STATUS BAR block", "STATUS BAR" not in m and ">5G<" not in m)
    check("header / orb / overlay preserved",
          all(s in m for s in ("J.A.R.V.I.S", "canvasRef", "INCOMING CALL")))
    check("never 'Friday'", "FRIDAY" not in m.upper().replace("WAKE UP JARVIS", ""))
    check("greeting addresses 'sir', not 'Boss'",
          "Welcome back, sir" in m and "Welcome back, Boss" not in m)

    print("\n[3] real backend wiring (not the mockup scenarios)")
    for ep in ("/chat", "/voice/stt", "/voice/tts/stream", "/events/ws",
               "/messaging/inbox", "/memory/graph", "/calls/command"):
        check(f"wired {ep}", ep in m)
    check("incoming-call overlay bound to real caller",
          all(b in m for b in ("{{ callInitial }}", "{{ callName }}", "{{ callNumber }}")))
    check("call buttons route to the command queue",
          'this._callAction("answer")' in m and 'this._callAction("decline")' in m)
    check("comms badge + memory graph bound",
          "{{ inboxCount }}" in m and "{{ memStat }}" in m and "{{ memSvgRef }}" in m)
    check("no template tokens leaked", not any(t in m for t in ("@@MARKUP@@", "@@DCATTRS@@", "@@PATCHED@@")))

    print("\n[4] patched dc-script parses as valid JS")
    dc = re.search(r'<script\s+[^>]*data-dc-script[^>]*>(.*?)</script>', m, re.DOTALL)
    check("dc-script recovered from mobile.html", bool(dc))
    if dc:
        js = "class DCLogic { setState(){} }\n" + dc.group(1)
        tmp = Path(tempfile.mkdtemp()) / "mobile_component.js"
        tmp.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
        check("node --check passes", r.returncode == 0, (r.stderr or "").strip()[:300])
        # the override methods (last-def-wins) and the original render must both be present
        check("override + render present",
              "_callAction(action)" in js and "renderVals()" in js and "_initEvents()" in js)

    print("\n[5] routing: phones -> mobile.html, desktop -> identical index.html")
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
    ANDROID = ("Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/124.0 Mobile Safari/537.36")
    DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/124.0.0.0 Safari/537.36")
    mobile_bytes = mobile_path.read_bytes()
    desktop_bytes = index_path.read_bytes()

    r_i = client.get("/", headers={"user-agent": IPHONE})
    check("iPhone gets the mobile UI", r_i.status_code == 200 and r_i.content == mobile_bytes)
    r_a = client.get("/", headers={"user-agent": ANDROID})
    check("Android gets the mobile UI", r_a.status_code == 200 and r_a.content == mobile_bytes)
    r_d = client.get("/", headers={"user-agent": DESKTOP})
    check("desktop gets the SAME index.html", r_d.status_code == 200 and r_d.content == desktop_bytes)
    r_force_d = client.get("/?ui=desktop", headers={"user-agent": IPHONE})
    check("?ui=desktop forces the HUD on a phone", r_force_d.content == desktop_bytes)
    r_force_m = client.get("/?ui=mobile", headers={"user-agent": DESKTOP})
    check("?ui=mobile previews the phone UI on desktop", r_force_m.content == mobile_bytes)

    print(f"\n==== mobile smoke: {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
