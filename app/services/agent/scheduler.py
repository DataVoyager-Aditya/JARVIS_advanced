"""
Persistent reminder/timer scheduler.

Reminders persist in SQLite (database/reminders.db) so they survive a restart. A background
async loop ticks every few seconds; when one is due it fires every registered callback — by
default a real Windows toast, and (when the voice listener is running) a spoken alert.

`parse_when` turns "in 5 minutes" / "at 8pm" / "tomorrow 9am" / "20:30" into an absolute
fire time, and always reports the resolved clock time back so JARVIS can state it (you should
never be left wondering whether an alarm was actually set).
"""

from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import logging
import re
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from config import DATABASE_DIR, BASE_DIR

logger = logging.getLogger("jarvis.scheduler")

_DB = DATABASE_DIR / "reminders.db"


# --------------------------------------------------------------------------- #
# when-parser
# --------------------------------------------------------------------------- #
_REL = re.compile(r"\b(?:in\s+)?(\d+)\s*(sec(?:ond)?s?|min(?:ute)?s?|hours?|hrs?|h|m|s)\b", re.I)
_AT = re.compile(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)
_HHMM = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", re.I)
_BARE_AMPM = re.compile(r"\b(\d{1,2})\s*(am|pm)\b", re.I)


def parse_when(text: str, now: _dt.datetime | None = None) -> tuple[_dt.datetime, str] | None:
    """Return (fire_datetime, human_clock_string) or None if unparseable."""
    now = now or _dt.datetime.now()
    t = text.lower().strip()
    tomorrow = "tomorrow" in t

    m = _REL.search(t)
    if m and "at" not in t[:m.start()]:
        n = int(m.group(1))
        unit = m.group(2)
        secs = n * (1 if unit.startswith("s") else 3600 if unit.startswith(("h", "hr")) else 60)
        fire = now + _dt.timedelta(seconds=secs)
        return fire, fire.strftime("%I:%M %p").lstrip("0")

    hh = mm = None
    ampm = None
    for rx in (_AT, _HHMM, _BARE_AMPM):
        m = rx.search(t)
        if m:
            hh = int(m.group(1))
            if rx is _BARE_AMPM:
                mm, ampm = 0, m.group(2)
            else:
                mm = int(m.group(2)) if m.group(2) else 0
                ampm = m.group(3)
            break
    if hh is None:
        return None
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hh != 12:
            hh += 12
        elif ampm == "am" and hh == 12:
            hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    fire = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if tomorrow:
        fire += _dt.timedelta(days=1)
    elif fire <= now:
        fire += _dt.timedelta(days=1)   # next occurrence
    return fire, fire.strftime("%I:%M %p").lstrip("0")


# --------------------------------------------------------------------------- #
# scheduler
# --------------------------------------------------------------------------- #
@dataclass
class Reminder:
    id: int
    fire_at: float        # epoch seconds
    text: str
    kind: str             # "timer" | "reminder"


FireCallback = Callable[[Reminder], "Awaitable[None] | None"]

_ALARM_SCRIPT = BASE_DIR / "scripts" / "alarm_fire.py"


def _pythonw() -> str:
    """The windowless interpreter next to the current one (no console flash)."""
    exe = Path(sys.executable)
    pw = exe.with_name("pythonw.exe")
    return str(pw if pw.exists() else exe)


def _register_os_task(rid: int, fire_at: _dt.datetime, kind: str, text: str) -> bool:
    """Register the alarm with WINDOWS TASK SCHEDULER so it fires even if JARVIS is closed.
    Returns True on success. Current-user task — no admin needed."""
    if sys.platform != "win32":
        return False
    b64 = base64.b64encode((text or "Alarm").encode("utf-8")).decode("ascii")
    at = fire_at.strftime("%Y-%m-%dT%H:%M:%S")
    name = f"JARVIS_Alarm_{rid}"
    ps = (
        f'$a = New-ScheduledTaskAction -Execute "{_pythonw()}" '
        f"-Argument '\"{_ALARM_SCRIPT}\" {b64} {kind}'; "
        f'$t = New-ScheduledTaskTrigger -Once -At "{at}"; '
        f'$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries '
        f'-StartWhenAvailable; '
        f'Register-ScheduledTask -TaskName "{name}" -Action $a -Trigger $t -Settings $s -Force | Out-Null'
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=25)
        if r.returncode == 0:
            return True
        logger.warning("OS alarm register failed (%s): %s", name, (r.stderr or "")[:160])
    except Exception as e:  # noqa: BLE001
        logger.warning("OS alarm register error: %s", e)
    return False


def _remove_os_task(rid: int) -> None:
    if sys.platform != "win32":
        return
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        f'Unregister-ScheduledTask -TaskName "JARVIS_Alarm_{rid}" -Confirm:$false'],
                       capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001
        pass


class Scheduler:
    def __init__(self) -> None:
        self._db = sqlite3.connect(str(_DB), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS reminders ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, fire_at REAL, text TEXT,"
            " kind TEXT, fired INTEGER DEFAULT 0)"
        )
        self._db.commit()
        self._lock = threading.Lock()
        # OS Task Scheduler fires the toast+sound (even when JARVIS is off), so the
        # in-process callbacks are just the spoken alert. _toast_fire is a fallback used
        # only when OS registration failed for a given reminder.
        # In-process firing is the reliable path while JARVIS runs: toast + (listener adds)
        # voice. The OS Task Scheduler task is the backup for when JARVIS is closed.
        self._callbacks: list[FireCallback] = [_toast_fire]
        self._task: asyncio.Task | None = None
        self.last_fired: Reminder | None = None   # for snooze / "stop the alarm"

    # -- registration --
    def on_fire(self, cb: FireCallback) -> None:
        self._callbacks.append(cb)

    def add(self, fire_at: _dt.datetime, text: str, kind: str = "reminder") -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO reminders(fire_at, text, kind, fired) VALUES(?,?,?,0)",
                (fire_at.timestamp(), text, kind),
            )
            self._db.commit()
            rid = cur.lastrowid
        # Register the OS-level backup alarm in a thread — PowerShell is slow and must NOT
        # block the async scheduler (that was making fires get missed).
        threading.Thread(target=_register_os_task, args=(rid, fire_at, kind, text), daemon=True).start()
        return rid

    def list_pending(self) -> list[Reminder]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, fire_at, text, kind FROM reminders WHERE fired=0 ORDER BY fire_at"
            ).fetchall()
        return [Reminder(*r) for r in rows]

    def cancel(self, rid: int) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE reminders SET fired=1 WHERE id=? AND fired=0", (rid,))
            self._db.commit()
            ok = cur.rowcount > 0
        if ok:
            threading.Thread(target=_remove_os_task, args=(rid,), daemon=True).start()
        return ok

    def _mark_fired(self, rid: int) -> None:
        with self._lock:
            self._db.execute("UPDATE reminders SET fired=1 WHERE id=?", (rid,))
            self._db.commit()

    def cancel_all(self) -> int:
        n = 0
        for r in self.list_pending():
            if self.cancel(r.id):
                n += 1
        return n

    @staticmethod
    def stop_ringing() -> None:
        """Silence a currently-ringing alarm by clearing JARVIS's toast notifications."""
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:  # noqa: BLE001
            pass
        if sys.platform == "win32":
            ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
                  " ContentType=WindowsRuntime] | Out-Null;"
                  " [Windows.UI.Notifications.ToastNotificationManager]::History.Clear('JARVIS')")
            try:
                subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                               capture_output=True, timeout=10)
            except Exception:  # noqa: BLE001
                pass

    # -- background loop --
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info("Scheduler loop started (%d pending)", len(self.list_pending()))

    async def _loop(self) -> None:
        while True:
            now = _dt.datetime.now().timestamp()
            for r in self.list_pending():
                if r.fire_at <= now:
                    logger.info("Reminder #%d firing: %s", r.id, r.text)
                    self._mark_fired(r.id)
                    self.last_fired = r
                    # JARVIS is running, so we fire in-process (toast + voice). Remove the
                    # OS backup task off-thread so it doesn't also fire a duplicate.
                    threading.Thread(target=_remove_os_task, args=(r.id,), daemon=True).start()
                    for cb in self._callbacks:
                        try:
                            res = cb(r)
                            if asyncio.iscoroutine(res):
                                await res
                        except Exception:  # noqa: BLE001
                            logger.exception("reminder callback failed")
            await asyncio.sleep(1)


def _toast_fire(r: Reminder) -> None:
    """Default fire action: a real Windows toast with the looping alarm sound."""
    title = "JARVIS — Timer" if r.kind == "timer" else "JARVIS — Reminder"

    def _pop():
        try:
            from win11toast import toast
            toast(title, r.text, app_id="JARVIS", duration="long",
                  audio={"src": "ms-winsoundevent:Notification.Looping.Alarm"})
        except Exception as e:  # noqa: BLE001
            logger.warning("toast failed (%s): %s", e, r.text)

    threading.Thread(target=_pop, daemon=True).start()


_singleton: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _singleton
    if _singleton is None:
        _singleton = Scheduler()
    return _singleton
