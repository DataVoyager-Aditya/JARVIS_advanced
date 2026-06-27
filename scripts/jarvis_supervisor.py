"""
JARVIS — headless always-on supervisor (Phase 10.L).

This is the background process that makes JARVIS *always there*. Launched at logon by Task
Scheduler under **pythonw** (no console window), it:

  1. Starts the backend (uvicorn) and the voice listener as windowless child processes.
  2. Health-monitors both — if the backend stops answering /health, or either child dies, it
     restarts that child with exponential backoff (so a crash loop can't peg the CPU).
  3. Publishes a heartbeat (child pids + last health) to database/runtime/status.json for the tray.
  4. Honours file-based control signals: a stop request shuts everything down cleanly; a restart
     request bounces the children. The tray and `--stop` write those flags.

It is the headless background process of PLANNER §10.L — no window, no UI, survives nothing-on-
screen, and a single-instance lock means logon can't accidentally start two of them.

Why a logon process and not a classic Windows service: JARVIS needs the microphone, speakers and
camera, which live in the *user's* desktop session. A session-0 service (the nssm path) is walled
off from those devices since Vista. A per-user logon task is the correct, free, no-admin way to be
truly always-on with full audio/vision access.

Usage:
    pythonw scripts/jarvis_supervisor.py            # run the supervisor (what Task Scheduler calls)
    python  scripts/jarvis_supervisor.py            # same, but with a console (handy for debugging)
    python  scripts/jarvis_supervisor.py --status    # print the current heartbeat and exit
    python  scripts/jarvis_supervisor.py --stop       # ask a running supervisor to shut down
    python  scripts/jarvis_supervisor.py --no-voice   # backend only (don't manage the listener)
    python  scripts/jarvis_supervisor.py --whatsapp   # also bring up the WhatsApp sidecar (decoupled)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import (  # noqa: E402
    RUNTIME_DIR,
    ALWAYSON_HEALTH_URL,
    ALWAYSON_POLL_S,
    ALWAYSON_HEALTH_FAILS,
    ALWAYSON_RESTART_BACKOFF_S,
    ALWAYSON_RESTART_BACKOFF_MAX_S,
)
from app.services import runtime  # noqa: E402

IS_WIN = platform.system() == "Windows"
HOST = "127.0.0.1"
PORT = 8000
# CREATE_NO_WINDOW keeps a child console-less even when this supervisor is itself run from a
# console (dev) — so under pythonw at logon nothing ever flashes on screen.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0


def _setup_logging() -> logging.Logger:
    log = logging.getLogger("jarvis.supervisor")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fh = RotatingFileHandler(Path(RUNTIME_DIR) / "supervisor.log",
                                 maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        log.addHandler(fh)
        # Also echo to the console when there is one (running under python.exe, not pythonw).
        if sys.stderr is not None:
            sh = logging.StreamHandler()
            sh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s",
                                              datefmt="%H:%M:%S"))
            log.addHandler(sh)
    return log


logger = _setup_logging()


def _port_serving(host: str, port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class Child:
    """A managed child process (backend or listener): how to (re)launch it, its log file, and the
    backoff state so a crash loop doesn't hammer the machine."""

    def __init__(self, name: str, argv: list[str], *, health: bool = False, attached: bool = False):
        self.name = name
        self.argv = argv
        self.health = health            # restart on /health failure, not just on process death
        self.attached = attached        # we found it already running — monitor, don't own/kill it
        self.proc: subprocess.Popen | None = None
        self.log_fh = None
        self.fails = 0                  # consecutive health misses
        self.restarts = 0               # for backoff
        self.next_attempt = 0.0         # monotonic time before which we won't relaunch
        self.last_start = 0.0

    # -- lifecycle -------------------------------------------------- #
    def start(self) -> None:
        if self.attached:
            return
        self.stop()                     # ensure no orphan
        self.log_fh = open(Path(RUNTIME_DIR) / f"{self.name}.log", "a", encoding="utf-8",
                           errors="replace")
        self.log_fh.write(f"\n===== {self.name} started {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        self.log_fh.flush()
        kw: dict = {"cwd": str(ROOT), "stdout": self.log_fh, "stderr": subprocess.STDOUT}
        if IS_WIN:
            kw["creationflags"] = _NO_WINDOW
        else:
            kw["start_new_session"] = True
        self.proc = subprocess.Popen(self.argv, **kw)
        self.last_start = time.monotonic()
        logger.info("%s started (pid %s)", self.name, self.proc.pid)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self.proc = None
        if self.log_fh:
            try:
                self.log_fh.close()
            except Exception:  # noqa: BLE001
                pass
            self.log_fh = None

    # -- state ------------------------------------------------------ #
    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def alive(self) -> bool:
        if self.attached:
            return True
        return bool(self.proc and self.proc.poll() is None)

    def supervise(self, healthy: bool) -> None:
        """Called each tick. Restarts the child if it died or (when health-gated) went unhealthy,
        with exponential backoff so a crash loop can't peg the CPU. A sustained-healthy stretch
        forgets past crashes (backoff resets)."""
        if self.attached:
            return
        now = time.monotonic()
        needs_restart = False

        if not self.alive():
            needs_restart = True
        elif self.health:
            if healthy:
                self.fails = 0
                if self.restarts and (now - self.last_start) > 60:   # been good a while
                    self.restarts = 0
            else:
                self.fails += 1
                if self.fails >= ALWAYSON_HEALTH_FAILS:
                    logger.warning("%s failed health %d× — bouncing", self.name, self.fails)
                    self.fails = 0
                    self.stop()
                    needs_restart = True

        if needs_restart and now >= self.next_attempt:
            self.restarts += 1
            delay = min(ALWAYSON_RESTART_BACKOFF_S * (2 ** (self.restarts - 1)),
                        ALWAYSON_RESTART_BACKOFF_MAX_S)
            self.next_attempt = now + delay            # earliest the NEXT restart may happen
            logger.warning("%s down — restart attempt %d (next backoff %.0fs)",
                           self.name, self.restarts, delay)
            self.start()


class Supervisor:
    def __init__(self, *, voice: bool = True, whatsapp: bool = False, ngrok: bool = False):
        self.voice = voice
        self.whatsapp = whatsapp
        self.ngrok = ngrok
        self.children: list[Child] = []
        self._stopping = False

    def _build_children(self, py: str) -> None:
        # Backend: attach if something already serves the port (e.g. a dev run_pwa), else spawn.
        if _port_serving(HOST, PORT):
            logger.info("backend already serving %s:%d — attaching (won't manage it)", HOST, PORT)
            self.children.append(Child("backend", [], health=True, attached=True))
        else:
            self.children.append(Child(
                "backend",
                [py, "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(PORT),
                 "--log-level", "info"],
                health=True,
            ))
        # Tunnel: a permanent ngrok URL so the phone (calls companion + mobile PWA) can reach the
        # backend even headless. Managed like any child (restart-on-crash, windowless). Killing it
        # on stop is safe — ngrok holds no session state. Needs NGROK_DOMAIN in .env.
        if self.ngrok:
            tunnel = self._ngrok_child()
            if tunnel:
                self.children.append(tunnel)
        if self.voice:
            self.children.append(Child("listener", [py, "scripts/jarvis_listener.py"]))

    def _ngrok_child(self) -> "Child | None":
        domain = os.getenv("NGROK_DOMAIN", "").strip().replace("https://", "").rstrip("/")
        if not domain:
            logger.warning("--ngrok set but NGROK_DOMAIN is empty in .env — skipping the tunnel "
                           "(phone-reachable features won't work headless until it's set)")
            return None
        import shutil
        ngrok = shutil.which("ngrok") or str(ROOT / "bin" / ("ngrok.exe" if IS_WIN else "ngrok"))
        if not (shutil.which("ngrok") or Path(ngrok).exists()):
            logger.warning("--ngrok set but the ngrok binary isn't installed — skipping the tunnel")
            return None
        logger.info("tunnel: exposing backend at https://%s", domain)
        return Child("tunnel", [ngrok, "http", f"--domain={domain}", str(PORT), "--log=stdout"])

    def _maybe_start_whatsapp(self) -> None:
        """Bring up the WhatsApp sidecar DECOUPLED (its own session must outlive us; hard-killing
        it corrupts the login). Mirrors run_pwa's behaviour — hidden if a session already exists."""
        if not self.whatsapp:
            return
        wa_dir = ROOT / "sidecars" / "whatsapp"
        if _port_serving(HOST, 3001):
            logger.info("WhatsApp sidecar already on :3001 — reusing it")
            return
        import shutil
        node = shutil.which("node")
        if not node or not (wa_dir / "node_modules").exists():
            logger.info("WhatsApp sidecar: node / node_modules missing — skipping")
            return
        session_exists = (wa_dir / ".wwebjs_auth" / "session").exists()
        kw: dict = {"cwd": str(wa_dir)}
        if IS_WIN:
            kw["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
                                   if session_exists else subprocess.CREATE_NEW_CONSOLE)
        else:
            kw["start_new_session"] = True
        subprocess.Popen([node, "index.js"], **kw)
        logger.info("WhatsApp sidecar launched (%s)", "hidden" if session_exists else "QR window")

    # -- run loop --------------------------------------------------- #
    def run(self) -> int:
        if not runtime.acquire_lock():
            logger.error("another JARVIS supervisor is already running (pid %s) — exiting",
                         runtime.lock_holder())
            return 2
        # A fresh run ignores any stale stop flag left from a previous shutdown.
        runtime.clear_stop()
        runtime.clear_restart()

        py = runtime.find_project_python()
        if not py:
            logger.error("no Python with project deps (uvicorn) found — cannot start backend")
            runtime.release_lock()
            return 3
        logger.info("supervisor up (pid %d) — interpreter %s", os.getpid(), py)

        self._install_signal_handlers()
        self._maybe_start_whatsapp()
        self._build_children(py)
        for c in self.children:
            c.start()

        try:
            while not self._stopping:
                if runtime.stop_requested():
                    logger.info("stop requested — shutting down")
                    runtime.clear_stop()
                    break
                if runtime.restart_requested():
                    logger.info("restart requested — bouncing children")
                    runtime.clear_restart()
                    for c in self.children:
                        if not c.attached:
                            c.stop()
                            c.restarts = 0
                            c.next_attempt = 0.0
                            c.start()
                healthy = runtime.health_ok(ALWAYSON_HEALTH_URL)
                for c in self.children:
                    c.supervise(healthy)
                self._publish(healthy)
                time.sleep(ALWAYSON_POLL_S)
        except KeyboardInterrupt:
            logger.info("interrupted")
        finally:
            self._shutdown()
        return 0

    def _publish(self, healthy: bool) -> None:
        runtime.write_status({
            "supervisor_pid": os.getpid(),
            "healthy": healthy,
            "muted": runtime.is_muted(),
            "voice": self.voice,
            "children": [
                {"name": c.name, "pid": c.pid, "alive": c.alive(),
                 "attached": c.attached, "restarts": c.restarts}
                for c in self.children
            ],
        })

    def _shutdown(self) -> None:
        self._stopping = True
        logger.info("stopping children …")
        for c in self.children:
            c.stop()
        runtime.write_status({"supervisor_pid": None, "healthy": False, "stopped": True,
                              "children": []})
        runtime.release_lock()
        logger.info("supervisor stopped")

    def _install_signal_handlers(self) -> None:
        def _handle(signum, _frame):
            logger.info("signal %s — stopping", signum)
            self._stopping = True
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handle)
            except (ValueError, OSError):
                pass
        if IS_WIN and hasattr(signal, "SIGBREAK"):
            try:
                signal.signal(signal.SIGBREAK, _handle)  # type: ignore[attr-defined]
            except (ValueError, OSError):
                pass


# ------------------------------------------------------------------ #
def _print_status() -> int:
    st = runtime.read_status()
    if not st:
        print("JARVIS supervisor: no status file — not running (or never started).")
        return 1
    fresh = runtime.status_is_fresh(st)
    running = runtime.supervisor_running()
    print(json.dumps(st, indent=2))
    print(f"\nsupervisor pid {st.get('supervisor_pid')} — "
          f"{'RUNNING' if running else 'not running'}, heartbeat "
          f"{'fresh' if fresh else 'STALE'}")
    return 0 if running else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="JARVIS always-on supervisor (Phase 10.L)")
    ap.add_argument("--stop", action="store_true", help="ask a running supervisor to shut down")
    ap.add_argument("--status", action="store_true", help="print the heartbeat and exit")
    ap.add_argument("--no-voice", action="store_true", help="don't manage the voice listener")
    ap.add_argument("--whatsapp", action="store_true", help="also bring up the WhatsApp sidecar")
    ap.add_argument("--ngrok", action="store_true",
                    help="also open the permanent ngrok tunnel (NGROK_DOMAIN in .env) so the phone "
                         "can reach the backend headless (calls companion + mobile PWA)")
    args = ap.parse_args()

    if args.status:
        return _print_status()
    if args.stop:
        if not runtime.supervisor_running():
            print("No running JARVIS supervisor to stop.")
            return 1
        runtime.request_stop()
        print("Stop requested — the supervisor will shut JARVIS down within a few seconds.")
        return 0

    return Supervisor(voice=not args.no_voice, whatsapp=args.whatsapp, ngrok=args.ngrok).run()


if __name__ == "__main__":
    sys.exit(main())
