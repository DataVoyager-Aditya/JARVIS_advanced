"""
JARVIS — auto-start installer (Phase 10.L).

Registers JARVIS to come up by itself the moment you log in — no admin, no window, no terminal.
It creates a per-user **Task Scheduler** task with a *logon* trigger that launches the headless
supervisor (`jarvis_supervisor.py`) under **pythonw** (console-less). Optionally it also drops a
hidden launcher for the system-tray app into your Startup folder.

Why a logon task (not a service): JARVIS needs the mic/speakers/camera, which only exist in your
desktop session — a session-0 Windows service can't reach them, and installing one needs admin.
A logon task runs in your session with full device access and needs no elevation.

Usage (run with the project venv Python):
    python scripts/jarvis_autostart.py --install            # backend + voice, at every logon
    python scripts/jarvis_autostart.py --install --tray     # also auto-start the tray app
    python scripts/jarvis_autostart.py --install --whatsapp # also bring up the WhatsApp sidecar
    python scripts/jarvis_autostart.py --install --no-voice # backend only (no listener)
    python scripts/jarvis_autostart.py --start              # start it NOW (no reboot needed)
    python scripts/jarvis_autostart.py --status             # is it installed / running?
    python scripts/jarvis_autostart.py --uninstall          # remove auto-start
    python scripts/jarvis_autostart.py --print-xml          # dump the task XML (dry run)

Nothing here touches the desktop HUD or any UI — it only schedules the background process.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import ALWAYSON_TASK_NAME, RUNTIME_DIR  # noqa: E402
from app.services import runtime  # noqa: E402

IS_WIN = platform.system() == "Windows"
SUPERVISOR = ROOT / "scripts" / "jarvis_supervisor.py"
TRAY = ROOT / "scripts" / "jarvis_tray.py"
TASK_XML_PATH = Path(RUNTIME_DIR) / "jarvis_task.xml"
TRAY_VBS_NAME = "JARVIS-Tray.vbs"


def current_user() -> str:
    """DOMAIN\\User (or COMPUTERNAME\\User on a non-domain PC) for the task principal/trigger."""
    dom = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
    user = os.environ.get("USERNAME") or ""
    return f"{dom}\\{user}" if dom else user


def supervisor_args(*, voice: bool, whatsapp: bool, ngrok: bool = False) -> str:
    args = []
    if not voice:
        args.append("--no-voice")
    if whatsapp:
        args.append("--whatsapp")
    if ngrok:
        args.append("--ngrok")
    return " ".join(args)


def build_task_xml(*, command: str, arguments: str, workdir: str, userid: str) -> str:
    """A Task Scheduler 1.2 definition: trigger at this user's logon, run console-less, never
    time-limited, don't stop on battery, and let Task Scheduler restart it 3× if it ever crashes
    (belt-and-braces on top of the supervisor's own child-restart). LeastPrivilege => no admin."""
    c, a, w, u = (escape(command), escape(arguments), escape(workdir), escape(userid))
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Author>JARVIS</Author>\n"
        "    <Description>JARVIS always-on supervisor (Phase 10.L) — starts the headless backend "
        "+ voice listener at logon.</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        f"      <UserId>{u}</UserId>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{u}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <IdleSettings>\n"
        "      <StopOnIdleEnd>false</StopOnIdleEnd>\n"
        "      <RestartOnIdle>false</RestartOnIdle>\n"
        "    </IdleSettings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>true</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "    <RestartOnFailure>\n"
        "      <Interval>PT1M</Interval>\n"
        "      <Count>3</Count>\n"
        "    </RestartOnFailure>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{c}</Command>\n"
        f"      <Arguments>{a}</Arguments>\n"
        f"      <WorkingDirectory>{w}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _resolve_pythonw() -> str:
    py = runtime.find_project_python()
    if not py:
        return ""
    return runtime.to_pythonw(py)


def _xml_for(voice: bool, whatsapp: bool, ngrok: bool = False) -> str:
    pyw = _resolve_pythonw() or "pythonw.exe"
    extra = supervisor_args(voice=voice, whatsapp=whatsapp, ngrok=ngrok)
    arguments = f'"{SUPERVISOR}"' + (f" {extra}" if extra else "")
    return build_task_xml(command=pyw, arguments=arguments, workdir=str(ROOT), userid=current_user())


# ------------------------------------------------------------------ #
def _startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _install_tray() -> None:
    """Drop a hidden VBS launcher for the tray into the Startup folder (runs pythonw with window
    style 0 = hidden, so the tray icon appears with no flashing console)."""
    pyw = _resolve_pythonw() or "pythonw.exe"
    vbs = (
        'Set s = CreateObject("WScript.Shell")\r\n'
        f's.Run """{pyw}"" ""{TRAY}""", 0, False\r\n'
    )
    dest = _startup_dir() / TRAY_VBS_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(vbs, encoding="utf-8")
    print(f"[autostart] tray auto-start installed -> {dest}")


def _uninstall_tray() -> None:
    dest = _startup_dir() / TRAY_VBS_NAME
    if dest.exists():
        dest.unlink()
        print(f"[autostart] tray auto-start removed -> {dest}")


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True)


def do_install(voice: bool, whatsapp: bool, tray: bool, ngrok: bool = False) -> int:
    if not IS_WIN:
        print("[autostart] Auto-start is implemented for Windows (Task Scheduler). On Linux/macOS "
              "use a systemd --user unit / launchd plist running scripts/jarvis_supervisor.py.")
        return 1
    if not _resolve_pythonw():
        print("[autostart] Could not find a Python with the project deps (uvicorn). Run this with "
              "the project venv, e.g. ..\\JARVIS\\.venv\\Scripts\\python.exe")
        return 2
    xml = _xml_for(voice, whatsapp, ngrok)
    TASK_XML_PATH.parent.mkdir(parents=True, exist_ok=True)
    # schtasks /Create /XML wants a Unicode (UTF-16) file.
    TASK_XML_PATH.write_text(xml, encoding="utf-16")
    r = _schtasks("/Create", "/TN", ALWAYSON_TASK_NAME, "/XML", str(TASK_XML_PATH), "/F")
    if r.returncode != 0:
        print("[autostart] schtasks /Create failed:")
        print((r.stdout or "") + (r.stderr or ""))
        return r.returncode
    print(f'[autostart] installed Scheduled Task "{ALWAYSON_TASK_NAME}" — JARVIS will start at '
          f"every logon (headless, no admin).")
    if tray:
        _install_tray()
    print("\n  Start it now without rebooting:  python scripts/jarvis_autostart.py --start")
    print("  Check status:                     python scripts/jarvis_autostart.py --status")
    return 0


def do_uninstall() -> int:
    if not IS_WIN:
        print("[autostart] Nothing to uninstall on this OS.")
        return 0
    # Stop a running supervisor first so it doesn't linger after the task is gone.
    if runtime.supervisor_running():
        runtime.request_stop()
    r = _schtasks("/Delete", "/TN", ALWAYSON_TASK_NAME, "/F")
    if r.returncode == 0:
        print(f'[autostart] removed Scheduled Task "{ALWAYSON_TASK_NAME}".')
    else:
        print(f'[autostart] task "{ALWAYSON_TASK_NAME}" was not installed (or already removed).')
    _uninstall_tray()
    return 0


def do_start() -> int:
    if not IS_WIN:
        print("[autostart] --start is Windows-only; run scripts/jarvis_supervisor.py directly.")
        return 1
    r = _schtasks("/Run", "/TN", ALWAYSON_TASK_NAME)
    if r.returncode == 0:
        print(f'[autostart] started "{ALWAYSON_TASK_NAME}". JARVIS is coming up in the background — '
              "say \"wake up jarvis\" in a few seconds.")
        return 0
    print("[autostart] couldn't start the task — is it installed? Run --install first.")
    print((r.stdout or "") + (r.stderr or ""))
    return r.returncode


def do_status() -> int:
    installed = False
    if IS_WIN:
        r = _schtasks("/Query", "/TN", ALWAYSON_TASK_NAME)
        installed = r.returncode == 0
        print(f'Scheduled Task "{ALWAYSON_TASK_NAME}": {"INSTALLED" if installed else "not installed"}')
        if installed:
            print(r.stdout.strip())
    running = runtime.supervisor_running()
    st = runtime.read_status()
    print(f"\nSupervisor: {'RUNNING (pid %s)' % runtime.lock_holder() if running else 'not running'}")
    if st:
        kids = ", ".join(f"{c['name']}={'up' if c['alive'] else 'down'}" for c in st.get("children", []))
        print(f"  last heartbeat: healthy={st.get('healthy')} muted={st.get('muted')} [{kids}]")
    return 0 if (installed or running) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="JARVIS auto-start installer (Phase 10.L)")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--start", action="store_true", help="start the installed task now (no reboot)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--print-xml", action="store_true", help="print the task XML and exit (dry run)")
    ap.add_argument("--no-voice", action="store_true", help="backend only (don't manage the listener)")
    ap.add_argument("--whatsapp", action="store_true", help="also start the WhatsApp sidecar")
    ap.add_argument("--ngrok", action="store_true",
                    help="also open the permanent ngrok tunnel so the phone reaches the backend "
                         "headless (calls companion + mobile PWA); needs NGROK_DOMAIN in .env")
    ap.add_argument("--tray", action="store_true", help="also auto-start the system-tray app")
    args = ap.parse_args()

    if args.print_xml:
        print(_xml_for(voice=not args.no_voice, whatsapp=args.whatsapp, ngrok=args.ngrok))
        return 0
    if args.uninstall:
        return do_uninstall()
    if args.start:
        return do_start()
    if args.status:
        return do_status()
    if args.install:
        return do_install(voice=not args.no_voice, whatsapp=args.whatsapp, tray=args.tray,
                          ngrok=args.ngrok)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
