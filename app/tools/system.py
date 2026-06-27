"""System tools — status, screenshot, media keys, volume. All local, free."""

from __future__ import annotations

import datetime as _dt
import logging

from config import AUDIO_TMP_DIR
from app.tools import tool

logger = logging.getLogger("jarvis.tools.system")


@tool("Report the PC's system status: CPU, RAM, battery, disk.", narration="Running diagnostics")
def system_status() -> str:
    import psutil
    parts = [f"CPU {psutil.cpu_percent(interval=0.3):.0f}%",
             f"RAM {psutil.virtual_memory().percent:.0f}%"]
    try:
        b = psutil.sensors_battery()
        if b is not None:
            parts.append(f"battery {b.percent:.0f}%{' (charging)' if b.power_plugged else ''}")
    except Exception:  # noqa: BLE001
        pass
    try:
        disk_pct = psutil.disk_usage("C:\\").percent
        parts.append(f"disk {disk_pct:.0f}% used")
    except Exception:  # noqa: BLE001
        pass
    return ", ".join(parts) + "."


@tool("Take a screenshot of the screen and save it. Returns the file path.",
      narration="Taking a screenshot")
def take_screenshot() -> str:
    import mss
    path = AUDIO_TMP_DIR.parent / "screenshots"
    path.mkdir(parents=True, exist_ok=True)
    fname = path / f"shot_{_dt.datetime.now():%Y%m%d_%H%M%S}.png"
    with mss.mss() as sct:
        sct.shot(mon=-1, output=str(fname))
    return f"Screenshot saved to {fname}."


@tool(
    "Control media playback (the active media app / browser).",
    params={"action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop"],
                       "description": "the playback action"}},
    required=["action"],
    narration="",
)
def media_control(action: str) -> str:
    import keyboard
    keymap = {"play_pause": "play/pause media", "next": "next track",
              "previous": "previous track", "stop": "stop media"}
    k = keymap.get(action)
    if not k:
        return f"Unknown media action '{action}'."
    keyboard.send(k)
    return f"Sent {action.replace('_', '/')}."


@tool(
    "Adjust system volume.",
    params={"action": {"type": "string", "enum": ["up", "down", "mute"]},
            "steps": {"type": "integer", "description": "how many increments for up/down (default 4)"}},
    required=["action"],
    narration="",
)
def set_volume(action: str, steps: int = 4) -> str:
    import keyboard
    if action == "mute":
        keyboard.send("volume mute")
        return "Toggled mute."
    if action in ("up", "down"):
        key = "volume up" if action == "up" else "volume down"
        for _ in range(max(1, min(steps, 20))):
            keyboard.send(key)
        return f"Volume {action} {steps} steps."
    return f"Unknown volume action '{action}'."
