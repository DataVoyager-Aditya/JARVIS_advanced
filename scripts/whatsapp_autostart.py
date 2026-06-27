"""
Make the WhatsApp sidecar ALWAYS-ON (Phase 7 / always-on requirement).

Drops a tiny hidden launcher into your Windows **Startup folder**, so the whatsapp-web.js
sidecar starts automatically and invisibly every time you sign in, reusing the saved login —
you scan the QR exactly once, ever, and WhatsApp stays permanently connected after that.

This uses the Startup folder (no admin rights needed) rather than Task Scheduler — schtasks
can hit "Access is denied" on some machines.

Usage (run once, from the project root):
    python scripts/whatsapp_autostart.py install     # turn auto-start ON
    python scripts/whatsapp_autostart.py status      # is it set up / running?
    python scripts/whatsapp_autostart.py uninstall   # turn it OFF

You must have scanned the QR once already (you have). After 'install', it also starts the
sidecar now if it isn't already running.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WA_DIR = ROOT / "sidecars" / "whatsapp"
STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
LAUNCHER = STARTUP_DIR / "JARVIS WhatsApp.vbs"


def _node() -> str:
    node = shutil.which("node")
    if not node:
        sys.exit("Node.js not found on PATH. Install Node, then re-run.")
    return node


def _port_open(port: int = 3001) -> bool:
    s = socket.socket(); s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", port)); return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        s.close()


def _vbs_text(node: str) -> str:
    # '0' = run with NO window (hidden); False = don't wait. CurrentDirectory makes the
    # session path (.wwebjs_auth) resolve correctly.
    return (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.CurrentDirectory = "{WA_DIR}"\r\n'
        f'sh.Run """{node}"" index.js", 0, False\r\n'
    )


def install() -> None:
    if os.name != "nt":
        sys.exit("This auto-start helper is Windows-only.")
    if not STARTUP_DIR.exists():
        sys.exit(f"Couldn't find the Startup folder at {STARTUP_DIR}")
    if not (WA_DIR / "node_modules").exists():
        sys.exit("Run `npm install` in sidecars/whatsapp first.")
    node = _node()
    LAUNCHER.write_text(_vbs_text(node), encoding="utf-8")
    print(f"[autostart] installed -> {LAUNCHER}")
    print("[autostart] WhatsApp will now start hidden at every Windows logon, reusing your login.")

    if _port_open():
        print("[autostart] sidecar already running on :3001 — leaving it.")
    else:
        subprocess.Popen(["wscript", str(LAUNCHER)])
        print("[autostart] started it now (hidden, in the background).")
    print("\nThat's it — permanent. Close nothing; it runs invisibly. To stop auto-start:")
    print("    python scripts/whatsapp_autostart.py uninstall")


def uninstall() -> None:
    if LAUNCHER.exists():
        try:
            LAUNCHER.unlink()
            print(f"[autostart] removed {LAUNCHER}")
        except Exception as e:  # noqa: BLE001
            sys.exit(f"Couldn't remove the launcher: {e}")
    else:
        print("[autostart] wasn't installed (no launcher found).")
    print("[autostart] WhatsApp won't auto-start anymore. A running sidecar keeps going until")
    print("            you close it / reboot (taskkill /F /IM node.exe to stop it now).")


def status() -> None:
    installed = LAUNCHER.exists()
    print(f"  Auto-start launcher: {'INSTALLED' if installed else 'not installed'}"
          + (f"  ({LAUNCHER})" if installed else ""))
    print(f"  Sidecar on :3001:    {'RUNNING' if _port_open() else 'not running'}")
    if not installed:
        print("  -> enable with:  python scripts/whatsapp_autostart.py install")


if __name__ == "__main__":
    action = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if action == "install":
        install()
    elif action == "uninstall":
        uninstall()
    elif action == "status":
        status()
    else:
        print("Usage: python scripts/whatsapp_autostart.py [install | status | uninstall]")
