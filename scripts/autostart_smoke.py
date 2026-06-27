"""
Phase 10.L smoke test — Always-on (auto-start + headless background).

Verifies everything checkable WITHOUT actually installing the logon task or rebooting: the runtime
coordination module (status heartbeat, mic-mute, stop/restart signals, single-instance lock,
process-liveness, health probe, interpreter discovery), the supervisor's child-command building +
crash-restart wiring, the autostart task-XML generation (logon trigger, no-admin, restart-on-fail,
hidden, correct interpreter + args), the tray's state→colour logic + icon drawing, and the listener
mic-mute wiring. The real "reboot and say wake up jarvis" check is the one live step left to Aditya.

Run:  python scripts/autostart_smoke.py    (use the project venv Python)
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))   # import sibling scripts (supervisor/autostart/tray)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "OK " if cond else "XX "
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {mark}{name}" + (f"  — {detail}" if detail and not cond else ""))


def main() -> None:
    from app.services import runtime

    # ---- isolate runtime state in a temp dir so the smoke never touches the real one ---- #
    tmp = Path(tempfile.mkdtemp())
    runtime.ALWAYSON_STATUS_FILE = tmp / "status.json"
    runtime.ALWAYSON_MUTE_FLAG = tmp / "mic.muted"
    runtime.ALWAYSON_STOP_FLAG = tmp / "stop.request"
    runtime.ALWAYSON_RESTART_FLAG = tmp / "restart.request"
    runtime.ALWAYSON_LOCK_FILE = tmp / "supervisor.lock"

    print("\n[1] runtime: status heartbeat write/read + freshness")
    check("no status initially", runtime.read_status() is None)
    runtime.write_status({"supervisor_pid": 4242, "healthy": True, "children": []})
    st = runtime.read_status()
    check("status roundtrips", bool(st) and st["supervisor_pid"] == 4242 and st["healthy"] is True)
    check("status carries a ts", isinstance(st.get("ts"), (int, float)))
    check("fresh status is fresh", runtime.status_is_fresh(st, max_age_s=30))
    check("old status is stale", not runtime.status_is_fresh({"ts": time.time() - 999}, max_age_s=30))
    check("None status never fresh", not runtime.status_is_fresh(None))

    print("\n[2] runtime: mic-mute flag")
    check("unmuted initially", not runtime.is_muted())
    runtime.set_muted(True)
    check("set_muted(True) -> muted", runtime.is_muted())
    runtime.set_muted(False)
    check("set_muted(False) -> unmuted", not runtime.is_muted())
    new = runtime.toggle_muted()
    check("toggle flips to muted", new is True and runtime.is_muted())
    check("toggle flips back", runtime.toggle_muted() is False and not runtime.is_muted())

    print("\n[3] runtime: stop / restart signals")
    check("no stop initially", not runtime.stop_requested())
    runtime.request_stop()
    check("request_stop sets it", runtime.stop_requested())
    runtime.clear_stop()
    check("clear_stop clears it", not runtime.stop_requested())
    runtime.request_restart()
    check("request_restart sets it", runtime.restart_requested())
    runtime.clear_restart()
    check("clear_restart clears it", not runtime.restart_requested())

    print("\n[4] runtime: process liveness + single-instance lock")
    check("our own pid is alive", runtime.process_alive(os.getpid()))
    check("a bogus pid is not alive", not runtime.process_alive(2_000_000_123))
    check("None pid is not alive", not runtime.process_alive(None))
    check("no lock holder initially", runtime.lock_holder() is None)
    check("supervisor not running initially", not runtime.supervisor_running())
    check("acquire_lock succeeds", runtime.acquire_lock())
    check("lock holder is us", runtime.lock_holder() == os.getpid())
    check("supervisor_running True while we hold (alive pid)", runtime.supervisor_running())
    # A second acquire by the SAME process is fine (re-entrant); a stale lock gets stolen.
    runtime.ALWAYSON_LOCK_FILE.write_text("2000000123", encoding="utf-8")   # dead pid
    check("acquire steals a stale (dead-pid) lock", runtime.acquire_lock() and runtime.lock_holder() == os.getpid())
    runtime.release_lock()
    check("release drops the lock", runtime.lock_holder() is None and not runtime.supervisor_running())

    print("\n[5] runtime: health probe + interpreter discovery")
    check("health_ok returns a bool (no raise)", isinstance(runtime.health_ok(timeout=0.4), bool))
    py = runtime.find_project_python()
    check("find_project_python returns a string", isinstance(py, str))
    if py:
        check("...and it's a real python path", Path(py).exists() and "python" in Path(py).name.lower())
        pyw = runtime.to_pythonw(py)
        if Path(py).name.lower() == "python.exe" and Path(py).with_name("pythonw.exe").exists():
            check("to_pythonw swaps to the windowless pythonw", pyw.lower().endswith("pythonw.exe"))
        else:
            check("to_pythonw returns a string", isinstance(pyw, str))
    else:
        print("     (no deps-python found in this env — run with the project venv to verify launch)")

    print("\n[6] supervisor: child-command building + crash-restart wiring")
    import jarvis_supervisor as sup
    sup._port_serving = lambda h, p: False        # force the spawn path (deterministic)
    s = sup.Supervisor(voice=True)
    s._build_children("PYX")
    names = [c.name for c in s.children]
    check("builds backend + listener", names == ["backend", "listener"], f"got {names}")
    backend = s.children[0]
    check("backend spawns uvicorn app.main:app",
          "uvicorn" in backend.argv and "app.main:app" in backend.argv and not backend.attached)
    check("backend is health-gated", backend.health is True)
    check("listener runs jarvis_listener.py", "jarvis_listener.py" in " ".join(s.children[1].argv))
    s2 = sup.Supervisor(voice=False)
    s2._build_children("PYX")
    check("--no-voice omits the listener", [c.name for c in s2.children] == ["backend"])
    sup._port_serving = lambda h, p: True         # something already serving -> attach, don't own
    s3 = sup.Supervisor(voice=False)
    s3._build_children("PYX")
    check("attaches to an already-running backend", s3.children[0].attached and s3.children[0].argv == [])
    # Backoff: a child that keeps dying schedules a growing delay (never relaunches faster than it).
    c = sup.Child("backend", ["PYX"], health=True)
    c.alive = lambda: False                        # pretend it's dead
    c.start = lambda: None                         # don't actually spawn
    c.supervise(healthy=False)
    first = c.restarts
    c.next_attempt = 0.0                           # allow the next attempt immediately
    c.supervise(healthy=False)
    check("crash-loop backoff increments restarts", c.restarts == first + 1 and first == 1)
    check("Windows children get the no-window flag",
          (sup._NO_WINDOW != 0) if os.name == "nt" else (sup._NO_WINDOW == 0))
    # Tunnel (--ngrok): no NGROK_DOMAIN -> degrade gracefully (no tunnel child, no crash).
    os.environ.pop("NGROK_DOMAIN", None)
    sup._port_serving = lambda h, p: False
    s4 = sup.Supervisor(voice=False, ngrok=True)
    s4._build_children("PYX")
    check("--ngrok without NGROK_DOMAIN degrades (no tunnel child)",
          [c.name for c in s4.children] == ["backend"] and s4._ngrok_child() is None)

    print("\n[7] autostart: logon task XML generation (no install)")
    import jarvis_autostart as au
    xml = au.build_task_xml(command=r"C:\py\pythonw.exe", arguments=r'"C:\j\scripts\jarvis_supervisor.py"',
                            workdir=r"C:\j", userid=r"PC\Aditya")
    check("XML is a LogonTrigger task", "<LogonTrigger>" in xml and "Task version=\"1.2\"" in xml)
    check("XML runs without admin (LeastPrivilege)", "<RunLevel>LeastPrivilege</RunLevel>" in xml)
    check("XML is hidden + unlimited runtime", "<Hidden>true</Hidden>" in xml and "<ExecutionTimeLimit>PT0S" in xml)
    check("XML restarts on failure", "<RestartOnFailure>" in xml and "<Count>3</Count>" in xml)
    check("XML survives on battery", "<DisallowStartIfOnBatteries>false" in xml and "<StopIfGoingOnBatteries>false" in xml)
    check("XML carries the command + workdir", r"C:\py\pythonw.exe" in xml and "<WorkingDirectory>C:\\j</WorkingDirectory>" in xml)
    check("XML carries the user", r"PC\Aditya" in xml)
    # XML is well-formed (strip the <?xml?> decl so ElementTree doesn't choke on the encoding attr).
    import xml.etree.ElementTree as ET
    try:
        ET.fromstring(xml.split("?>", 1)[1])
        check("task XML is well-formed", True)
    except Exception as e:  # noqa: BLE001
        check("task XML is well-formed", False, str(e))
    check("current_user is non-empty", bool(au.current_user()))
    check("supervisor_args maps flags",
          au.supervisor_args(voice=False, whatsapp=True) == "--no-voice --whatsapp"
          and au.supervisor_args(voice=True, whatsapp=False) == "")
    x_nv = au._xml_for(voice=False, whatsapp=False)
    x_wa = au._xml_for(voice=True, whatsapp=True)
    check("--no-voice flows into the task args", "--no-voice" in x_nv)
    check("--whatsapp flows into the task args", "--whatsapp" in x_wa)
    check("task args quote the supervisor script path", "jarvis_supervisor.py" in x_nv)
    check("--ngrok maps into supervisor args + task XML",
          au.supervisor_args(voice=True, whatsapp=False, ngrok=True) == "--ngrok"
          and "--ngrok" in au._xml_for(voice=True, whatsapp=False, ngrok=True))

    print("\n[8] tray: state -> colour logic + icon")
    import jarvis_tray as tray
    # Offline (no lock/status in the temp dir).
    runtime.write_status({"supervisor_pid": None, "healthy": False, "children": []})
    summ = tray.status_summary()
    check("offline -> red", summ["state"] == "offline" and summ["color"] == tray._RED)
    # Simulate a live, healthy supervisor: we hold the lock (our pid is alive) + a healthy heartbeat.
    runtime.acquire_lock()
    runtime.write_status({"supervisor_pid": os.getpid(), "healthy": True,
                          "children": [{"name": "backend", "alive": True}]})
    runtime.set_muted(False)
    summ = tray.status_summary()
    check("healthy + unmuted -> listening/green", summ["state"] == "listening" and summ["color"] == tray._GREEN)
    runtime.set_muted(True)
    summ = tray.status_summary()
    check("healthy + muted -> muted/cyan", summ["state"] == "muted" and summ["color"] == tray._CYAN)
    runtime.write_status({"supervisor_pid": os.getpid(), "healthy": False, "children": []})
    summ = tray.status_summary()
    check("running but unhealthy -> starting/amber", summ["state"] == "starting" and summ["color"] == tray._AMBER)
    runtime.set_muted(False)
    runtime.release_lock()
    if tray._PIL_OK:
        img = tray.draw_icon(tray._GREEN)
        check("draw_icon produces a 64x64 RGBA image", img.size == (64, 64) and img.mode == "RGBA")
    else:
        print("     (Pillow not present — icon drawing not checked; pip install pillow)")

    print("\n[9] listener: mic-mute wiring (static)")
    src = (ROOT / "scripts" / "jarvis_listener.py").read_text(encoding="utf-8")
    check("listener imports the runtime module", "from app.services import runtime" in src)
    check("listener honours runtime.is_muted() in the wake loop", "runtime.is_muted()" in src and "self._muted" in src)

    print("\n[10] config + module sanity")
    import config
    check("config exposes ALWAYSON_TASK_NAME", bool(getattr(config, "ALWAYSON_TASK_NAME", "")))
    check("config RUNTIME_DIR exists on disk", Path(config.RUNTIME_DIR).is_dir())
    check("supervisor exposes Supervisor + Child", hasattr(sup, "Supervisor") and hasattr(sup, "Child"))
    check("autostart exposes install/uninstall/status", all(hasattr(au, f) for f in ("do_install", "do_uninstall", "do_status")))

    print(f"\n==== autostart (10.L) smoke: {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
