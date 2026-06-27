"""Notes — a local dated markdown vault. Free, on-disk, greppable."""

from __future__ import annotations

import datetime as _dt

from config import DATABASE_DIR
from app.tools import tool

_VAULT = DATABASE_DIR / "notes"
_VAULT.mkdir(parents=True, exist_ok=True)


@tool(
    "Save a note / jot something down to remember later (a thought, idea, fact, to-do).",
    params={"text": {"type": "string", "description": "the note content"}},
    required=["text"],
    narration="Noting that down",
)
def note_write(text: str) -> str:
    day = _dt.date.today().isoformat()
    f = _VAULT / f"{day}.md"
    stamp = _dt.datetime.now().strftime("%I:%M %p").lstrip("0")
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(f"- {stamp} — {text.strip()}\n")
    return "Noted."


@tool(
    "Search your saved notes for a word or phrase.",
    params={"query": {"type": "string", "description": "text to look for in notes"}},
    required=["query"],
    narration="Searching your notes",
)
def note_search(query: str) -> str:
    q = query.lower().strip()
    hits = []
    for f in sorted(_VAULT.glob("*.md"), reverse=True):
        for line in f.read_text(encoding="utf-8").splitlines():
            if q in line.lower():
                hits.append(f"{f.stem}: {line.lstrip('- ').strip()}")
            if len(hits) >= 12:
                break
        if len(hits) >= 12:
            break
    return "\n".join(hits) if hits else f"No notes mention '{query}'."


@tool("Read today's notes.", narration="")
def note_today() -> str:
    f = _VAULT / f"{_dt.date.today().isoformat()}.md"
    if not f.exists():
        return "No notes today yet."
    return f.read_text(encoding="utf-8").strip() or "No notes today yet."
