"""
Phase 8 — fake an incoming call so you can test the whole call flow WITHOUT a phone.

It POSTs an /calls/incoming event (exactly what the Android companion would send), so JARVIS:
  - speaks "Sir, <name> is calling ..." on the PC (if the listener is running), and
  - shows the incoming-call card in the PWA (if it's open).

Then it watches the command queue for ~45 s: decline / answer / silence the call by VOICE
("wake up jarvis, decline the call") or by TAPPING the PWA card — this script prints what it
sees. If nothing acts, it marks the call missed so you can then ask "any missed calls?".

Usage:
  python scripts/calls_demo.py                 # caller "Mom"
  python scripts/calls_demo.py "Vikram" +15551234
"""

from __future__ import annotations

import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"
TOKEN = "jarvis-local-calls"   # = CALLS_WEBHOOK_TOKEN in .env (change if you changed it)


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "Mom"
    number = sys.argv[2] if len(sys.argv) > 2 else "+15550100"
    ref = f"demo-{int(time.time())}"
    h = {"x-jarvis-token": TOKEN}

    try:
        r = httpx.post(f"{BASE}/calls/incoming",
                       json={"number": number, "name": name, "ref": ref}, headers=h, timeout=5)
    except Exception as e:  # noqa: BLE001
        print(f"!! Could not reach {BASE} — is JARVIS running? ({e})")
        sys.exit(1)
    if r.status_code == 401:
        print("!! 401 — token mismatch. Set TOKEN in this script to your CALLS_WEBHOOK_TOKEN.")
        sys.exit(1)
    print(f"\n📞 Ringing: {name} ({number})")
    print("   -> JARVIS should ANNOUNCE it on the PC, and the PWA should show the call card.")
    print("   Now: say 'wake up jarvis, decline the call' (or tap the PWA card).")
    print("   Watching for your command for 45s...\n")

    acted = None
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            c = httpx.get(f"{BASE}/calls/commands", headers=h, timeout=4).json().get("commands", [])
        except Exception:  # noqa: BLE001
            c = []
        if c:
            acted = c[0].get("action")
            print(f"   ✅ command received from JARVIS: {acted.upper()}")
            break
        time.sleep(1.5)

    if acted:
        print(f"\n   (The real companion would now {acted} the call on your phone.)")
        httpx.post(f"{BASE}/calls/ended", json={"number": number, "ref": ref,
                   "answered": acted == "answer"}, headers=h, timeout=5)
    else:
        print("\n   No command in time — marking it a MISSED call.")
        httpx.post(f"{BASE}/calls/missed", json={"number": number, "name": name,
                   "ref": f"{ref}-miss"}, headers=h, timeout=5)
        print('   Now ask JARVIS: "any missed calls?" — he should read it back.')
    print()


if __name__ == "__main__":
    main()
