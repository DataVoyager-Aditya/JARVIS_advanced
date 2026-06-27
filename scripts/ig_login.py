"""
One-time Instagram login for JARVIS (Phase 7).

Goal: produce a saved instagrapi session (config.IG_SESSION_PATH) so JARVIS can use Instagram
silently afterwards — no repeated logins/codes.

Two methods, in order of reliability:

  1. SESSION COOKIE (recommended — works with 2FA, "approve on your phone", AND when your IP
     has been temporarily flagged by the login endpoint): log into instagram.com in your
     browser, copy the `sessionid` cookie, paste it here. This reuses the session your browser
     already made, so it sidesteps the password-login flow entirely.

  2. PASSWORD login (fallback): uses IG_USERNAME / IG_PASSWORD from .env, with handlers for
     emailed/SMS codes and authenticator (TOTP) 2FA codes. Won't work if your only 2FA method
     is the in-app "Approve / Deny" prompt, or if Instagram has rate-flagged your IP.

Run it from the project root, in YOUR terminal:

    & "c:\\Users\\Lenovo\\Desktop\\JARVIS\\.venv\\Scripts\\python.exe" scripts\\ig_login.py

How to get the sessionid cookie (method 1):
  1. Open https://www.instagram.com in Chrome/Edge and make sure you're logged in.
  2. Press F12 -> "Application" tab -> left sidebar: Cookies -> https://www.instagram.com
  3. Find the row named `sessionid`. Copy its Value (a long string like 7251%3Aabcd...).
  4. Paste it here when asked. (It's a secret — treat it like a password.)
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import IG_USERNAME, IG_PASSWORD, IG_SESSION_PATH  # noqa: E402


def _code_handler(username, choice):
    where = str(getattr(choice, "name", choice)).lower()
    where = {"email": "email", "sms": "phone (SMS)"}.get(where, where)
    return input(f"\n  -> Enter the security code Instagram sent to your {where}: ").strip()


def _save_and_validate(cl, session: str) -> None:
    # Probe with an AUTHENTICATED call JARVIS actually uses (own account info), then the feed.
    # If either works, the session is genuinely logged in.
    last = None
    for probe in ("account_info", "get_timeline_feed"):
        try:
            getattr(cl, probe)()
            cl.dump_settings(session)
            print(f"\n[ig_login] SUCCESS - logged in as @{cl.username}")
            print(f"[ig_login] session saved to {session}")
            print("[ig_login] JARVIS will now use Instagram silently. You won't need to do this again.")
            print("[ig_login] You can now DELETE database/ig_sessionid.txt.")
            return
        except Exception as e:  # noqa: BLE001
            last = e

    sys.exit(
        f"\n[ig_login] The session was read but Instagram refused the authenticated call "
        f"({last}).\n"
        "           This is almost certainly the temporary IP cooldown from the earlier login\n"
        "           attempts — the sessionid itself is fine (your browser still works).\n"
        "           Fix: wait ~2-4 hours, OR run this once on a different network (phone\n"
        "           hotspot), with the SAME sessionid in database/ig_sessionid.txt. It'll go through.")


def _extract_sessionid(text: str) -> str:
    """Pull the bare sessionid out of whatever the user pasted/saved — tolerant of labels
    ('normal: ...'), surrounding quotes, a BOM, or multiple lines. A real sessionid looks like
    <digits>%3A<token>...  (or with ':' instead of %3A when URL-decoded)."""
    import re
    text = (text or "").lstrip("﻿").strip()
    m = re.search(r"\d{6,}(?:%3A|:)[A-Za-z0-9%_\-:.]+", text)
    if m:
        return m.group(0).strip().strip('"').strip("'")
    return text.strip().strip('"').strip("'")


def _read_sessionid() -> str:
    """Get the sessionid from (in order): IG_SESSIONID env, a database/ig_sessionid.txt file,
    or a VISIBLE terminal paste. The file path is the most reliable on Windows terminals that
    block pasting into prompts."""
    import os
    raw = os.getenv("IG_SESSIONID", "").strip()
    if raw:
        print("[ig_login] using sessionid from IG_SESSIONID env.")
        return _extract_sessionid(raw)
    sid_file = Path(IG_SESSION_PATH).parent / "ig_sessionid.txt"
    if sid_file.exists():
        raw = sid_file.read_text(encoding="utf-8-sig").strip()   # utf-8-sig strips a BOM
        if raw:
            sid = _extract_sessionid(raw)
            print(f"[ig_login] using sessionid from {sid_file} (parsed {len(sid)} chars).")
            return sid
    print("\n=== Method 1: session cookie (recommended; handles 2FA / IP flags) ===")
    print("  Get it: instagram.com (logged in) -> F12 -> Application -> Cookies ->")
    print("          https://www.instagram.com -> copy the 'sessionid' value.")
    print(f"  EASIEST: paste JUST the value into  {sid_file}  (save, nothing else), then re-run.")
    print("  Or paste it right here (it WILL show on screen — that's fine, it's local):")
    return _extract_sessionid(input("  sessionid (or press Enter to try password login): "))


def _login_by_sessionid(session: str) -> bool:
    from instagrapi import Client

    sid = _read_sessionid()
    if not sid:
        return False

    # Try both the URL-encoded (%3A) and decoded (:) forms, in both directions, each on a
    # FRESH client so a failed attempt doesn't taint the next.
    candidates: list[str] = []
    for c in (sid, sid.replace("%3A", ":"), sid.replace(":", "%3A")):
        if c and c not in candidates:
            candidates.append(c)

    last = None
    for i, cand in enumerate(candidates, 1):
        cl = Client()
        cl.delay_range = [1, 3]
        try:
            form = "encoded %3A" if "%3A" in cand else "decoded :"
            print(f"[ig_login] trying sessionid ({form}, attempt {i}/{len(candidates)}) ...")
            cl.login_by_sessionid(cand)
            _save_and_validate(cl, session)
            return True
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"[ig_login]   that form failed: {e}")
    raise last if last else RuntimeError("sessionid login failed")


def _login_by_password(cl) -> None:
    from instagrapi.exceptions import TwoFactorRequired, ChallengeRequired, ClientError
    user = IG_USERNAME or input("Instagram username: ").strip()
    pwd = IG_PASSWORD or getpass.getpass("Instagram password: ")
    if not user or not pwd:
        sys.exit("No Instagram credentials in .env, and none entered.")
    cl.challenge_code_handler = _code_handler
    print(f"\n=== Method 2: password login as @{user} ===")
    twofa = input("  If you use an AUTHENTICATOR APP or SMS code 2FA, type the current 6-digit "
                  "code now, or press Enter: ").strip()
    try:
        cl.login(user, pwd, verification_code=twofa) if twofa else cl.login(user, pwd)
    except TwoFactorRequired:
        code = input("  -> 2FA code required (from your authenticator app or SMS): ").strip()
        if not code:
            sys.exit("\n[ig_login] Your 2FA seems to be the in-app 'Approve/Deny' type, which "
                     "has no code.\n           Re-run and use Method 1 (sessionid cookie) instead.")
        cl.login(user, pwd, verification_code=code)
    except ChallengeRequired:
        sys.exit("\n[ig_login] Instagram wants you to approve this login.\n"
                 "           Open the Instagram app, approve 'Was this you?', then re-run —\n"
                 "           or just use Method 1 (sessionid cookie), which avoids this.")
    except ClientError as e:
        sys.exit(f"[ig_login] login failed: {e}")


def main() -> None:
    from instagrapi import Client

    session = str(IG_SESSION_PATH)

    try:
        if _login_by_sessionid(session):
            return
    except Exception as e:  # noqa: BLE001
        print(f"[ig_login] sessionid login failed ({e}).")
        print("[ig_login] If it says 'Invalid sessionid', re-copy the FULL sessionid value from")
        print("           the browser cookie (it should look like  NUM%3Alongtoken%3ANUM ).")
        print("[ig_login] Falling back to password login...\n")

    cl = Client()
    cl.delay_range = [1, 3]
    _login_by_password(cl)
    _save_and_validate(cl, session)


if __name__ == "__main__":
    main()
