"""
Phase 10.L — runtime coordination for the always-on JARVIS.

The headless supervisor, the voice listener and the (optional) system-tray app are three
separate processes. They coordinate through tiny files under `database/runtime/` instead of a
socket — robust, restart-proof, and zero-dependency:

  * status.json     — the supervisor's heartbeat (child pids + last health), read by the tray.
  * mic.muted       — a flag the tray sets; the listener honours it (ignores the wake word).
  * stop.request    — the tray (or `jarvis_supervisor.py --stop`) asks a running supervisor to exit.
  * restart.request — ask the supervisor to bounce the backend + listener.
  * supervisor.lock — single-instance guard (holds the live supervisor's pid).

Everything here is best-effort and never raises into a caller's hot path: a missing/locked/corrupt
file reads as "absent", so the worst case is the listener stays unmuted or the tray shows "unknown",
never a crash. Writes are atomic (temp + os.replace) so a reader never sees a half-written file.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from config import (
    BASE_DIR,
    ALWAYSON_STATUS_FILE,
    ALWAYSON_MUTE_FLAG,
    ALWAYSON_STOP_FLAG,
    ALWAYSON_RESTART_FLAG,
    ALWAYSON_LOCK_FILE,
    ALWAYSON_HEALTH_URL,
)

# ------------------------------------------------------------------ #
# Atomic file helpers
# ------------------------------------------------------------------ #
def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)              # atomic on Windows + POSIX
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ #
# Supervisor status (heartbeat) — written by the supervisor, read by the tray / `--status`
# ------------------------------------------------------------------ #
def write_status(status: dict) -> None:
    """Persist the supervisor heartbeat. Adds `ts` so a reader can tell if it's stale."""
    payload = dict(status)
    payload["ts"] = time.time()
    try:
        _atomic_write(Path(ALWAYSON_STATUS_FILE), json.dumps(payload, indent=2))
    except OSError:
        pass


def read_status() -> dict | None:
    """The last supervisor heartbeat, or None if there isn't one / it's unreadable."""
    try:
        return json.loads(Path(ALWAYSON_STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status_is_fresh(status: dict | None, max_age_s: float = 30.0) -> bool:
    """True if the heartbeat exists and was written within `max_age_s` (i.e. the supervisor is
    alive and ticking, not a leftover file from a dead run)."""
    if not status:
        return False
    ts = status.get("ts")
    return isinstance(ts, (int, float)) and (time.time() - ts) <= max_age_s


# ------------------------------------------------------------------ #
# Mic mute — the tray sets it; the listener checks it each wake-loop pass
# ------------------------------------------------------------------ #
def set_muted(muted: bool) -> None:
    flag = Path(ALWAYSON_MUTE_FLAG)
    try:
        if muted:
            flag.write_text(str(time.time()), encoding="utf-8")
        elif flag.exists():
            flag.unlink()
    except OSError:
        pass


def is_muted() -> bool:
    return Path(ALWAYSON_MUTE_FLAG).exists()


def toggle_muted() -> bool:
    """Flip the mute flag; returns the new muted state."""
    new = not is_muted()
    set_muted(new)
    return new


# ------------------------------------------------------------------ #
# Stop / restart requests — file-based signals the supervisor polls
# ------------------------------------------------------------------ #
def request_stop() -> None:
    try:
        Path(ALWAYSON_STOP_FLAG).write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def stop_requested() -> bool:
    return Path(ALWAYSON_STOP_FLAG).exists()


def clear_stop() -> None:
    try:
        Path(ALWAYSON_STOP_FLAG).unlink()
    except OSError:
        pass


def request_restart() -> None:
    try:
        Path(ALWAYSON_RESTART_FLAG).write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def restart_requested() -> bool:
    return Path(ALWAYSON_RESTART_FLAG).exists()


def clear_restart() -> None:
    try:
        Path(ALWAYSON_RESTART_FLAG).unlink()
    except OSError:
        pass


# ------------------------------------------------------------------ #
# Process liveness + single-instance lock
# ------------------------------------------------------------------ #
def process_alive(pid: int | None) -> bool:
    """Cross-platform 'is this pid running?'. Prefers psutil (a project dep); falls back to a
    Windows tasklist / POSIX os.kill probe so it still answers if psutil is missing."""
    if not pid or pid <= 0:
        return False
    try:
        import psutil  # type: ignore
        return psutil.pid_exists(int(pid))
    except Exception:  # noqa: BLE001
        pass
    if os.name == "nt":
        try:
            import subprocess
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            return str(int(pid)) in out
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True              # pid exists but is owned by another user => alive
    except OSError:
        return False


def lock_holder() -> int | None:
    """The pid recorded in the supervisor lock file, or None."""
    try:
        return int(Path(ALWAYSON_LOCK_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def supervisor_running() -> bool:
    """True if a supervisor is currently holding the lock AND its process is alive (a stale lock
    from a hard-killed run reads as not-running)."""
    return process_alive(lock_holder())


def acquire_lock() -> bool:
    """Claim the single-instance lock for the current process. Returns False if another LIVE
    supervisor already holds it; True (and writes our pid) otherwise — stealing a stale lock."""
    holder = lock_holder()
    if holder and holder != os.getpid() and process_alive(holder):
        return False
    try:
        _atomic_write(Path(ALWAYSON_LOCK_FILE), str(os.getpid()))
        return True
    except OSError:
        return False


def release_lock() -> None:
    """Drop the lock if we hold it (don't clobber another process's lock)."""
    if lock_holder() == os.getpid():
        try:
            Path(ALWAYSON_LOCK_FILE).unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ #
# Health probe — "is the backend actually answering?", not just "is the pid alive?"
# ------------------------------------------------------------------ #
def health_ok(url: str | None = None, timeout: float = 2.5) -> bool:
    try:
        import httpx
        r = httpx.get(url or ALWAYSON_HEALTH_URL, timeout=timeout)
        return r.status_code == 200 and r.json().get("status") == "ok"
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ #
# Interpreter discovery — find a Python that actually has the project deps
# ------------------------------------------------------------------ #
def find_project_python() -> str:
    """Path to a Python with the project deps (uvicorn). The project runs on the reused
    `..\\JARVIS\\.venv`; the bare system Python on PATH usually has no uvicorn. Returns "" if
    none can be found."""
    import subprocess
    root = Path(BASE_DIR)
    candidates = [
        sys.executable,
        str(root.parent / "JARVIS" / ".venv" / "Scripts" / "python.exe"),
        str(root / ".venv" / "Scripts" / "python.exe"),
        str(root / ".venv" / "bin" / "python"),
    ]
    seen: set[str] = set()
    for p in candidates:
        if not p or p in seen:
            continue
        seen.add(p)
        if p != sys.executable and not Path(p).exists():
            continue
        try:
            r = subprocess.run([p, "-c", "import uvicorn"], capture_output=True, timeout=25)
            if r.returncode == 0:
                return p
        except Exception:  # noqa: BLE001
            continue
    return ""


def to_pythonw(python_exe: str) -> str:
    """The windowless twin of a CPython launcher (python.exe -> pythonw.exe) so the supervisor
    runs with NO console. Falls back to the original path if pythonw isn't beside it."""
    if not python_exe:
        return python_exe
    p = Path(python_exe)
    if p.name.lower() == "python.exe":
        w = p.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return python_exe
