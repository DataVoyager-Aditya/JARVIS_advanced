"""
Personal profile loader — the single human-editable file of "everything about me".

`MY_PROFILE.md` lives in the project root. The boss fills it in once (name, where he lives,
preferences, the people in his life, his routines, what he's working on) and on every startup
JARVIS seeds those lines straight into his semantic memory (Tier 3). So they're known from the
very first word — no need to teach him by voice.

The file is the source of truth for the keys it contains: edit it, restart, and JARVIS is
updated. Anything he learns on the fly (via the `remember` tool / nightly consolidation) lives
alongside it and is untouched unless the file also names that key.

Format is deliberately unbreakable — plain `label: value` lines under `#` section headers.
Labels can be friendly ("Coffee order") or canonical dotted keys ("prefs.coffee_order");
both are slugified the same way. Lines starting with `#` are comments/headers and ignored.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("jarvis.memory.profile")

_PLACEHOLDER_HINTS = ("<", "your ", "e.g.", "...", "example")

_TEMPLATE = """# ───────────────────────────────────────────────────────────────
#  JARVIS — MY PROFILE
#  This is everything JARVIS permanently knows about you.
#  Fill in the lines that apply, delete the rest, then RESTART JARVIS.
#  Format:  Label: value      (anything after a # is ignored)
#  Add as many lines as you like under any section.
# ───────────────────────────────────────────────────────────────

# ── About you ──────────────────────────────────────────────────
user.full_name: Aditya Raj Thakur
# user.call_me: sir
# user.location: Bangalore, India
# user.birthday: 14 March
# user.occupation: software engineer

# ── Preferences (how you like things) ──────────────────────────
# prefs.coffee_order: black, no sugar
# prefs.wake_time: 6:30 am
# prefs.humor_level: dry and understated
# prefs.music: lo-fi while working

# ── People in your life ────────────────────────────────────────
# contacts.vikram.relation: younger brother
# contacts.vikram.location: Pune
# contacts.meera.relation: sister, a doctor in Delhi
# contacts.mom.note: always summarize and notify, never auto-reply

# ── Routines ───────────────────────────────────────────────────
# routines.gym_time: 7:00 am on weekdays
# routines.standup: 10:00 am daily
# routines.evening_walk: 7:30 pm

# ── Work & projects ────────────────────────────────────────────
# work.role: building JARVIS, a personal AI assistant
# work.project_atlas: data-pipeline rewrite, due next month

# ── Anything else worth remembering ────────────────────────────
# misc.dietary: vegetarian
# misc.cities_i_care_about: Bangalore, Pune, Delhi
"""


def ensure_template(path: Path) -> None:
    """Create the profile file with a friendly template if it doesn't exist yet."""
    if path.exists():
        return
    try:
        path.write_text(_TEMPLATE, encoding="utf-8")
        logger.info("created profile template at %s", path)
    except Exception as e:  # noqa: BLE001
        logger.warning("could not create profile template (%s)", e)


def _is_placeholder(value: str) -> bool:
    v = value.strip().lower()
    return v.startswith("<") or v.startswith("e.g.") or v in ("...", "—", "-")


def load_profile(semantic, path: Path) -> int:
    """Parse MY_PROFILE.md into semantic facts. Returns the count seeded. Never raises.

    Supports three natural shapes, so the boss can write his profile however reads best:
      1. Single fact:        `prefs.coffee_order: black, no sugar`
      2. Nested block:       `startup.stacy:`  then `role: Co-Founder` / `vision: ...`
                             -> seeds `startup.stacy.role`, `startup.stacy.vision`, ...
      3. Multi-line value:   `research.deepfake:`  then a paragraph on the next line(s)
                             -> seeds `research.deepfake` = that paragraph.

    A line ending in a bare colon (no value) OPENS a block; following `sub: value` lines
    become `block.sub`, and plain text lines accumulate into the block's own value. A blank
    line or a `#` section header CLOSES the block. Comments (`#`) are ignored throughout.
    """
    ensure_template(path)
    if not path.exists():
        return 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:  # noqa: BLE001
        logger.warning("could not read profile (%s)", e)
        return 0

    seeded = 0
    block: str | None = None       # current open block key, or None
    block_text: list[str] = []     # plain-text lines accumulated for the open block

    def put(key: str, value: str) -> None:
        nonlocal seeded
        if not key or not value or _is_placeholder(value):
            return
        try:
            semantic.set(key, value, source="profile")
            seeded += 1
        except Exception:  # noqa: BLE001
            logger.warning("skipped bad profile entry: %s", key)

    def close_block() -> None:
        nonlocal block, block_text
        if block and block_text:
            put(block, ", ".join(block_text))
        block, block_text = None, []

    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("//") or s.startswith("<!--"):
            close_block()
            continue
        s = s.lstrip("-*•> ").strip()           # tolerate markdown bullets / quotes
        if ":" in s:
            key, _, value = s.partition(":")
            key, value = key.strip(), value.strip()
            if not key:
                continue
            if value:                            # a real `key: value`
                put(f"{block}.{key}" if block else key, value)
            else:                                # bare `key:` opens a block
                close_block()
                block = key
        elif block:                              # plain text inside an open block
            block_text.append(s)

    close_block()
    if seeded:
        logger.info("profile loaded — %d fact(s) seeded from %s", seeded, path.name)
    return seeded
