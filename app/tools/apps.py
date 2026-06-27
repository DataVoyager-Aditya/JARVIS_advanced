"""App / browser tools — open apps, open URLs, play YouTube. Windows, local, free."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import webbrowser
from pathlib import Path

from app.tools import tool

logger = logging.getLogger("jarvis.tools.apps")

# Built into Windows — always available, always launched as a desktop app.
_SYSTEM_APPS = {
    "notepad": "notepad", "calculator": "calc", "calc": "calc", "paint": "mspaint",
    "explorer": "explorer", "files": "explorer", "file explorer": "explorer",
    "settings": "start ms-settings:", "task manager": "taskmgr", "cmd": "cmd",
    "command prompt": "cmd", "terminal": "wt", "powershell": "powershell",
}

# Third-party desktop apps -> launch command. Used ONLY if actually installed (checked via
# Start-Menu shortcut or PATH); otherwise we fall back to the web version below.
_DESKTOP_APPS = {
    "chrome": "chrome", "edge": "msedge", "firefox": "firefox",
    "vs code": "code", "vscode": "code", "code": "code",
    "spotify": "spotify", "telegram": "telegram", "whatsapp": "whatsapp",
    "steam": "steam", "discord": "discord", "slack": "slack",
}

# Common things people "open" that are websites, not installed apps.
_WEB_APPS = {
    "youtube": "https://www.youtube.com", "youtube music": "https://music.youtube.com",
    "gmail": "https://mail.google.com", "google": "https://www.google.com",
    "maps": "https://maps.google.com", "google maps": "https://maps.google.com",
    "drive": "https://drive.google.com", "calendar": "https://calendar.google.com",
    "chatgpt": "https://chat.openai.com", "github": "https://github.com",
    "netflix": "https://www.netflix.com", "prime video": "https://www.primevideo.com",
    "hotstar": "https://www.hotstar.com", "spotify web": "https://open.spotify.com",
    "whatsapp web": "https://web.whatsapp.com", "reddit": "https://www.reddit.com",
    "twitter": "https://twitter.com", "x": "https://x.com",
    "instagram": "https://www.instagram.com", "facebook": "https://www.facebook.com",
    "gemini": "https://gemini.google.com", "translate": "https://translate.google.com",
    # web versions of apps that also have a desktop client (used when not installed)
    "spotify": "https://open.spotify.com", "whatsapp": "https://web.whatsapp.com",
    "telegram": "https://web.telegram.org", "discord": "https://discord.com/app",
    "slack": "https://app.slack.com",
}

_START_MENU_DIRS = [
    Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
]


def _find_shortcut(name: str) -> Path | None:
    name_l = name.lower()
    for base in _START_MENU_DIRS:
        if not base.exists():
            continue
        for lnk in base.rglob("*.lnk"):
            if name_l in lnk.stem.lower():
                return lnk
    return None


@tool(
    "Open / launch an application on the PC by name (e.g. 'spotify', 'chrome', 'notepad', "
    "'telegram', 'vs code').",
    params={"name": {"type": "string", "description": "the app name to open"}},
    required=["name"],
    narration="Opening that for you",
)
def open_app(name: str) -> str:
    """Prefer the installed DESKTOP app; if it isn't installed, open the WEB version."""
    name = name.strip()
    low = name.lower()
    try:
        # 1. Windows built-in (always a desktop app).
        if low in _SYSTEM_APPS:
            cmd = _SYSTEM_APPS[low]
            (os.system if cmd.startswith("start ") else lambda c: subprocess.Popen(c, shell=True))(cmd)
            return f"Opened {name}."

        # 2. Installed desktop app? Prefer it. Detect via Start-Menu shortcut or PATH.
        lnk = _find_shortcut(name)
        if lnk:
            os.startfile(str(lnk))  # noqa: S606
            return f"Opened {name}."
        cmd = _DESKTOP_APPS.get(low)
        if cmd and shutil.which(cmd):
            subprocess.Popen(cmd, shell=True)
            return f"Opened {name}."

        # 3. Not installed as a desktop app -> web version if we know one.
        if low in _WEB_APPS:
            webbrowser.open(_WEB_APPS[low])
            return f"Opened {name} in your browser."

        # 4. Looks like a domain.
        if "." in low and " " not in low:
            webbrowser.open(low if low.startswith("http") else "https://" + low)
            return f"Opened {name}."

        # 5. Last resort: let Windows try to resolve it as an installed program.
        if os.system(f'start "" "{name}"') == 0:
            return f"Opened {name}."
        return f"I couldn't find an app or site called '{name}', sir."
    except Exception as e:  # noqa: BLE001
        return f"[couldn't open {name}: {e}]"


@tool(
    "Open a URL in the default web browser.",
    params={"url": {"type": "string", "description": "the full URL (https://...)"}},
    required=["url"],
    narration="Opening that link",
)
def open_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    webbrowser.open(url)
    return f"Opened {url}."


@tool(
    "Find and play a YouTube video by search query (opens it in the browser, autoplaying).",
    params={"query": {"type": "string", "description": "what to play, e.g. 'lofi hip hop' or 'CarryMinati latest'"}},
    required=["query"],
    narration="Pulling that up on YouTube",
)
def play_youtube(query: str) -> str:
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        entries = info.get("entries") or []
        if not entries:
            # fall back to opening a search page
            webbrowser.open(f"https://www.youtube.com/results?search_query={query}")
            return f"Couldn't resolve a single video; opened YouTube search for '{query}'."
        vid = entries[0]
        url = vid.get("url") or f"https://www.youtube.com/watch?v={vid.get('id')}"
        if not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"
        webbrowser.open(url)
        return f"Playing '{vid.get('title', query)}' on YouTube."
    except Exception as e:  # noqa: BLE001
        webbrowser.open(f"https://www.youtube.com/results?search_query={query}")
        return f"Opened YouTube search for '{query}' (resolve failed: {e})."
