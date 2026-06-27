"""
Fired BY WINDOWS TASK SCHEDULER at the alarm time — runs even if JARVIS is closed.

Pops a real Windows toast with the looping alarm sound. Text is passed base64-encoded so
there are no quoting headaches in the scheduled-task command line.

    pythonw alarm_fire.py <base64-text> <kind>
"""

import base64
import sys


def main() -> None:
    text = base64.b64decode(sys.argv[1]).decode("utf-8") if len(sys.argv) > 1 else "JARVIS alarm"
    kind = sys.argv[2] if len(sys.argv) > 2 else "reminder"
    title = "JARVIS — Timer" if kind == "timer" else "JARVIS — Reminder"
    try:
        from win11toast import toast
        toast(title, text, audio={"src": "ms-winsoundevent:Notification.Looping.Alarm"},
              duration="long", app_id="JARVIS")
    except Exception:
        # Last-resort fallback so the alarm is never silent.
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)


if __name__ == "__main__":
    main()
