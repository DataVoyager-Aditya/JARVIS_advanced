"""
Routines / macros — save a multi-step command under a name and replay it.

  "save this as 'evening routine'"  -> save_routine("evening routine", "open spotify, play
                                       lofi, set a 30 minute timer")
  "run my evening routine"          -> run_routine("evening routine") returns those steps;
                                       the agent then executes each real tool in turn.
"""

from __future__ import annotations

import sqlite3
import threading

from config import DATABASE_DIR
from app.tools import tool

_DB = sqlite3.connect(str(DATABASE_DIR / "routines.db"), check_same_thread=False)
_DB.execute("CREATE TABLE IF NOT EXISTS routines (name TEXT PRIMARY KEY, steps TEXT)")
_DB.commit()
_LOCK = threading.Lock()


@tool(
    "Save a named routine/macro: a sequence of actions to replay later. Pass the steps as a "
    "single instruction string (e.g. 'open spotify, play lofi, set a 30 minute timer').",
    params={"name": {"type": "string", "description": "the routine name, e.g. 'evening routine'"},
            "steps": {"type": "string", "description": "the actions, as one instruction line"}},
    required=["name", "steps"],
    narration="Saving that routine",
)
def save_routine(name: str, steps: str) -> str:
    name = name.lower().strip()
    with _LOCK:
        _DB.execute("INSERT INTO routines(name, steps) VALUES(?,?) "
                    "ON CONFLICT(name) DO UPDATE SET steps=excluded.steps", (name, steps.strip()))
        _DB.commit()
    return f"Saved the '{name}' routine."


@tool(
    "Run a saved routine by name. Returns its steps; then perform each step with the real "
    "tools, in order.",
    params={"name": {"type": "string", "description": "the routine name to run"}},
    required=["name"],
    narration="Running that routine",
)
def run_routine(name: str) -> str:
    name = name.lower().strip()
    with _LOCK:
        row = _DB.execute("SELECT steps FROM routines WHERE name=?", (name,)).fetchone()
    if not row:
        return f"No routine called '{name}'. Save one first."
    return f"Routine '{name}' steps to perform now, in order: {row[0]}"


@tool("List saved routines.", narration="")
def list_routines() -> str:
    with _LOCK:
        rows = _DB.execute("SELECT name, steps FROM routines ORDER BY name").fetchall()
    if not rows:
        return "No routines saved yet."
    return "\n".join(f"- {n}: {s}" for n, s in rows)
