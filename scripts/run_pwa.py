"""
Launch the JARVIS PWA for PC + phone (Phase 9).

Starts the backend (uvicorn) and a free Cloudflare Tunnel, giving you a public HTTPS URL
(https://<random>.trycloudflare.com). HTTPS is required so the phone can use the microphone
and install the app — plain http://<lan-ip> is not a secure context and the mic/SW won't work.

  python scripts/run_pwa.py            # backend + tunnel (downloads cloudflared once, free)
  python scripts/run_pwa.py --local    # backend only, no tunnel (open http://127.0.0.1:8000)

On a phone: open the printed trycloudflare URL → menu → "Add to Home screen" → JARVIS installs
as a standalone app. On PC: open it in Chrome/Edge → install icon in the address bar.

Nothing is exposed until YOU run this; the tunnel closes when you press Ctrl+C.
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
PORT = 8000
HOST = "127.0.0.1"

_CF_URLS = {
    ("Windows", "AMD64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
    ("Windows", "ARM64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-arm64.exe",
    ("Linux", "x86_64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
    ("Darwin", "arm64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz",
}


def _port_open(host: str, port: int) -> bool:
    """True if something is already listening on host:port (e.g. the WhatsApp sidecar)."""
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect((host, port))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        s.close()


def _python_with_uvicorn() -> str:
    """Find a Python that actually has the project deps. The project runs on the reused
    `..\\JARVIS\\.venv`; the bare system Python on PATH has no uvicorn."""
    candidates = [
        sys.executable,
        str(ROOT.parent / "JARVIS" / ".venv" / "Scripts" / "python.exe"),  # reused venv (Windows)
        str(ROOT / ".venv" / "Scripts" / "python.exe"),
        str(ROOT / ".venv" / "bin" / "python"),
    ]
    seen = set()
    for p in candidates:
        if not p or p in seen:
            continue
        seen.add(p)
        if p != sys.executable and not Path(p).exists():
            continue
        try:
            r = subprocess.run([p, "-c", "import uvicorn"], capture_output=True, timeout=20)
            if r.returncode == 0:
                return p
        except Exception:  # noqa: BLE001
            continue
    return ""


def ensure_cloudflared() -> str | None:
    import shutil
    existing = shutil.which("cloudflared")
    if existing:
        return existing
    BIN.mkdir(exist_ok=True)
    exe = BIN / ("cloudflared.exe" if platform.system() == "Windows" else "cloudflared")
    if exe.exists():
        return str(exe)
    key = (platform.system(), platform.machine())
    url = _CF_URLS.get(key)
    if not url:
        print(f"[run_pwa] No cloudflared download mapping for {key}.")
        print("          Install it from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/")
        return None
    if url.endswith(".tgz"):
        print("[run_pwa] On macOS install cloudflared via:  brew install cloudflared")
        return None
    print(f"[run_pwa] Downloading cloudflared (free, one-time) ...\n          {url}")
    import httpx
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        with open(exe, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    if platform.system() != "Windows":
        exe.chmod(0o755)
    print(f"[run_pwa] cloudflared ready -> {exe}")
    return str(exe)


def main() -> None:
    # Load .env so launcher-level config (NGROK_DOMAIN, etc.) is visible to THIS process — the
    # backend loads it via config.py, but run_pwa itself doesn't import config.
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true", help="no public tunnel (PC only)")
    ap.add_argument("--ngrok", action="store_true",
                    help="use a PERMANENT ngrok static domain instead of the random Cloudflare URL "
                         "(set NGROK_DOMAIN=your-name.ngrok-free.app in .env)")
    ap.add_argument("--no-voice", action="store_true", help="don't start the hands-free voice listener")
    ap.add_argument("--whatsapp", action="store_true",
                    help="also start the WhatsApp sidecar (needs `npm install` in sidecars/whatsapp first)")
    args = ap.parse_args()

    py = _python_with_uvicorn()
    if not py:
        print("[run_pwa] Couldn't find a Python with the project deps (uvicorn).")
        print(r'          Run it with the project venv, e.g.:')
        print(r'          & "c:\Users\Lenovo\Desktop\JARVIS\.venv\Scripts\python.exe" scripts\run_pwa.py --local')
        return
    print(f"[run_pwa] interpreter: {py}")
    print(f"[run_pwa] starting backend on http://{HOST}:{PORT} ...")
    backend = subprocess.Popen(
        [py, "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(PORT),
         "--log-level", "info"],
        cwd=str(ROOT),
    )
    listener = None
    try:
        time.sleep(2.5)
        if backend.poll() is not None:
            print("[run_pwa] backend failed to start — see the error above.")
            return

        # Optional WhatsApp sidecar (whatsapp-web.js). We start it DECOUPLED — in its own
        # window, NOT as a child we force-kill — because hard-killing it corrupts the WhatsApp
        # session (so you'd have to re-scan the QR every time). Run this way it keeps its login
        # across JARVIS restarts; you scan the QR once and close its window when you're done.
        if args.whatsapp:
            wa_dir = ROOT / "sidecars" / "whatsapp"
            if _port_open(HOST, 3001):
                print("[run_pwa] WhatsApp sidecar already running on :3001 — reusing it (stays linked).")
            elif not (wa_dir / "node_modules").exists():
                print("[run_pwa] WhatsApp sidecar: run `npm install` in sidecars/whatsapp first — skipping.")
            else:
                import shutil
                node = shutil.which("node")
                if not node:
                    print("[run_pwa] WhatsApp sidecar: Node.js not found on PATH — skipping.")
                else:
                    # If a login already exists, start it HIDDEN in the background (no window to
                    # accidentally close = WhatsApp can't get "turned off"). Only show a window
                    # the FIRST time, when a QR scan is actually needed. Either way it's detached
                    # so this launcher never force-kills it (that would corrupt the session).
                    session_exists = (wa_dir / ".wwebjs_auth" / "session").exists()
                    kw = {}
                    if platform.system() == "Windows":
                        if session_exists:
                            kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
                        else:
                            kw["creationflags"] = subprocess.CREATE_NEW_CONSOLE
                    else:
                        kw["start_new_session"] = True
                    subprocess.Popen([node, "index.js"], cwd=str(wa_dir), **kw)
                    if session_exists:
                        print("[run_pwa] WhatsApp sidecar started HIDDEN in the background (login found,")
                        print("          no QR, no window). Give it ~30s to warm up, then it just works.")
                    else:
                        print("[run_pwa] WhatsApp sidecar opened in its OWN window — scan the QR there ONCE.")
                        print("          After that first scan it'll start hidden automatically next time.")

        # Hands-free voice: the proven desktop listener (Vosk wake word + Whisper + Edge TTS).
        # It speaks through the PC and drives the PWA HUD live over /events. No clicking.
        if not args.no_voice:
            print("[run_pwa] starting hands-free voice listener — just say \"wake up jarvis\" ...")
            listener = subprocess.Popen(
                [py, "scripts/jarvis_listener.py"], cwd=str(ROOT),
            )

        if args.local:
            print(f"\n  JARVIS PWA (local):  http://{HOST}:{PORT}\n  Ctrl+C to stop.\n")
            backend.wait()
            return

        # PERMANENT URL via an ngrok reserved (static) domain — the URL never changes, so the
        # phone app + Macrodroid macros are set up ONCE. Free: one static domain per ngrok account.
        if args.ngrok:
            import os
            import shutil
            domain = os.getenv("NGROK_DOMAIN", "").strip().replace("https://", "").rstrip("/")
            ngrok = shutil.which("ngrok") or str(BIN / ("ngrok.exe" if platform.system() == "Windows" else "ngrok"))
            if not (shutil.which("ngrok") or Path(ngrok).exists()):
                print("\n[run_pwa] ngrok not found. One-time setup (free, permanent URL):")
                print("  1. Sign up at https://dashboard.ngrok.com  →  copy your authtoken")
                print("  2. Download ngrok, then:  ngrok config add-authtoken <YOUR_TOKEN>")
                print("  3. Dashboard → Domains → claim your free static domain (e.g. jarvis-xxx.ngrok-free.app)")
                print("  4. Put it in .env:  NGROK_DOMAIN=jarvis-xxx.ngrok-free.app")
                print("  Then re-run with --ngrok.  (Backend still live locally meanwhile.)\n")
                backend.wait()
                return
            if not domain:
                print("\n[run_pwa] --ngrok needs a reserved domain. Add to .env:")
                print("  NGROK_DOMAIN=your-name.ngrok-free.app   (claim one free at dashboard.ngrok.com → Domains)\n")
                backend.wait()
                return
            print("\n" + "=" * 60)
            print(f"  JARVIS permanent URL:  https://{domain}")
            print("  Same URL every time — set the phone up once.")
            print("=" * 60 + "\n")
            tunnel = subprocess.Popen([ngrok, "http", f"--domain={domain}", str(PORT), "--log=stdout"],
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(ROOT))
            try:
                for line in tunnel.stdout:  # type: ignore[union-attr]
                    print("  [ngrok] " + line.rstrip())
            except KeyboardInterrupt:
                pass
            finally:
                tunnel.terminate()
            return

        cf = ensure_cloudflared()
        if not cf:
            print(f"\n  Tunnel unavailable — backend still live at http://{HOST}:{PORT}")
            print("  (Phone needs HTTPS for mic/install; install cloudflared then re-run.)\n")
            backend.wait()
            return

        print("[run_pwa] opening Cloudflare Tunnel (watch for the https URL below) ...\n")
        tunnel = subprocess.Popen(
            [cf, "tunnel", "--url", f"http://{HOST}:{PORT}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=str(ROOT),
        )
        try:
            for line in tunnel.stdout:  # type: ignore[union-attr]
                line = line.rstrip()
                print("  [cf] " + line)
                if "trycloudflare.com" in line and "https://" in line:
                    url = line[line.find("https://"):].split()[0]
                    print("\n" + "=" * 60)
                    print(f"  JARVIS is live at:  {url}")
                    print("  Open it on your phone → Add to Home screen to install.")
                    print("=" * 60 + "\n")
        except KeyboardInterrupt:
            pass
        finally:
            tunnel.terminate()
    except KeyboardInterrupt:
        pass
    finally:
        # Note: the WhatsApp sidecar is intentionally NOT killed here — it runs decoupled in
        # its own window so its session survives (hard-killing it corrupts the login). Close
        # that window yourself to stop WhatsApp.
        if listener:
            listener.terminate()
        backend.terminate()
        print("\n[run_pwa] stopped.")


if __name__ == "__main__":
    main()
