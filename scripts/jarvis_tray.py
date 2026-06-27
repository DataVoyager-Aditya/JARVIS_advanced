"""
JARVIS — system-tray app (Phase 10.L, optional convenience).

A tiny arc-reactor icon in the Windows tray that shows JARVIS's live state and gives you one-click
control — WITHOUT being load-bearing. Killing the tray never stops JARVIS: the headless supervisor
(`jarvis_supervisor.py`) keeps the backend + voice listener running regardless. The tray only reads
the supervisor's heartbeat and writes the same file-based control flags the supervisor already polls.

Icon colour = state:  green = listening · cyan = muted · amber = starting/unhealthy · red = offline.

Menu:
  * Open JARVIS        — opens the HUD (Edge/Chrome app window if available, else default browser)
  * Mute / Unmute mic  — toggles the wake word (the listener honours runtime.is_muted())
  * Restart JARVIS     — bounces the backend + listener (supervisor handles it)
  * Quit JARVIS        — shuts the whole supervisor down (stops everything)
  * Close tray only    — removes just this icon; JARVIS keeps running

Run:  pythonw scripts/jarvis_tray.py     (the autostart installer drops a hidden launcher for this)
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import runtime  # noqa: E402

try:
    from PIL import Image, ImageDraw  # Pillow — already a project dep (icon generation)
    _PIL_OK = True
except Exception:  # noqa: BLE001
    _PIL_OK = False

HUD_URL = "http://127.0.0.1:8000/?ui=desktop"

# Status colour palette (R,G,B).
_CYAN = (90, 209, 255)
_GREEN = (57, 255, 120)
_AMBER = (255, 176, 0)
_RED = (255, 59, 48)


# ------------------------------------------------------------------ #
# Pure state logic (no pystray / no GUI) — unit-testable
# ------------------------------------------------------------------ #
def status_summary() -> dict:
    """Collapse the supervisor heartbeat into what the tray renders: a colour, a one-line title,
    and per-child detail. Pure read — safe to call from the smoke test."""
    running = runtime.supervisor_running()
    st = runtime.read_status()
    muted = runtime.is_muted()
    if not running:
        return {"state": "offline", "color": _RED, "title": "JARVIS — offline",
                "muted": muted, "children": []}
    healthy = bool(st and st.get("healthy"))
    children = (st or {}).get("children", [])
    if not healthy:
        return {"state": "starting", "color": _AMBER, "title": "JARVIS — starting…",
                "muted": muted, "children": children}
    if muted:
        return {"state": "muted", "color": _CYAN, "title": "JARVIS — mic muted",
                "muted": True, "children": children}
    return {"state": "listening", "color": _GREEN, "title": "JARVIS — listening",
            "muted": False, "children": children}


def draw_icon(color: tuple[int, int, int]):
    """An arc-reactor glyph (cyan ring + a status-coloured core) as a 64×64 RGBA image."""
    if not _PIL_OK:
        raise RuntimeError("Pillow not available")
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), outline=_CYAN, width=4)          # outer ring
    d.ellipse((16, 16, 48, 48), outline=(*_CYAN, 160), width=2)  # inner ring
    d.ellipse((24, 24, 40, 40), fill=color)                     # status core
    return img


# ------------------------------------------------------------------ #
# Actions
# ------------------------------------------------------------------ #
def open_hud() -> None:
    """Open the HUD in an app window (Edge/Chrome --app) so it looks like an installed app, not a
    browser tab; fall back to the default browser."""
    for name in ("msedge", "chrome"):
        exe = shutil.which(name)
        if exe:
            try:
                subprocess.Popen([exe, f"--app={HUD_URL}"])
                return
            except Exception:  # noqa: BLE001
                break
    webbrowser.open(HUD_URL)


# ------------------------------------------------------------------ #
# Tray (pystray) — only imported when actually running the icon
# ------------------------------------------------------------------ #
def main() -> int:
    if platform.system() != "Windows":
        print("[tray] The system tray is intended for Windows. (Backend/voice run anywhere.)")
    try:
        import pystray
    except Exception:  # noqa: BLE001
        print("[tray] pystray isn't installed. Install it (free):  pip install pystray pillow")
        print("       The tray is optional — JARVIS runs fine without it.")
        return 1
    if not _PIL_OK:
        print("[tray] Pillow isn't installed. Install it (free):  pip install pillow")
        return 1

    state = {"running": True}

    def _label(icon) -> str:
        return status_summary()["title"]

    def _is_muted(item) -> bool:
        return runtime.is_muted()

    def _toggle_mute(icon, item) -> None:
        runtime.toggle_muted()
        _refresh(icon)

    def _restart(icon, item) -> None:
        runtime.request_restart()

    def _quit_jarvis(icon, item) -> None:
        runtime.request_stop()              # stops the whole supervisor (backend + listener)
        state["running"] = False
        icon.stop()

    def _close_tray(icon, item) -> None:
        state["running"] = False
        icon.stop()                          # JARVIS keeps running; only the icon goes away

    def _open(icon, item) -> None:
        open_hud()

    menu = pystray.Menu(
        pystray.MenuItem(_label, None, enabled=False),          # live status line (disabled)
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open JARVIS", _open, default=True),
        pystray.MenuItem("Mute mic", _toggle_mute, checked=_is_muted),
        pystray.MenuItem("Restart JARVIS", _restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit JARVIS (stop everything)", _quit_jarvis),
        pystray.MenuItem("Close tray only", _close_tray),
    )

    icon = pystray.Icon("jarvis", draw_icon(status_summary()["color"]), "JARVIS", menu)

    def _refresh(ic) -> None:
        summ = status_summary()
        try:
            ic.icon = draw_icon(summ["color"])
            ic.title = summ["title"]
            ic.update_menu()
        except Exception:  # noqa: BLE001
            pass

    def _poll() -> None:
        while state["running"]:
            _refresh(icon)
            time.sleep(3.0)

    threading.Thread(target=_poll, daemon=True).start()
    icon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
