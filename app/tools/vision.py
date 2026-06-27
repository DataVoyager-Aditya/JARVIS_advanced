"""
Vision tools (Phase 2) — JARVIS's eyes for the agent.

  read_screen(question?)   — look at the PC screen (what's open, read text/errors, OCR)
  look(question?)          — grab a webcam frame and identify what the boss is showing
  describe_image(path, q?) — describe a local image file

All run on the agent's event loop, so the async VisionService is driven via a worker thread.
Each returns a ready-to-speak sentence and degrades in character (never an exception) if the
screen/camera/model isn't available. Results are complete confirmations → terminal=True (the
agent speaks them directly, no extra rephrase round-trip).
"""

from __future__ import annotations

import logging

from app.tools import tool

logger = logging.getLogger("jarvis.tools.vision")


def _run(coro):
    """Drive an async VisionService coroutine to completion from a sync tool (own loop in a
    worker thread, like the messaging tools)."""
    import asyncio
    import threading
    box: dict = {}

    def runner():
        try:
            box["v"] = asyncio.run(coro)
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=runner)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["v"]


@tool(
    "Look at the boss's PC SCREEN and answer about it — what app/window is open, read on-screen "
    "text, an error message, a document, or anything visible. Use for 'what's on my screen', "
    "'read this', 'what does this error say', 'help me with what I'm looking at'. Don't use it "
    "for the camera/physical objects (that's `look`).",
    params={"question": {"type": "string",
                         "description": "what he wants to know about the screen (optional)"}},
    narration="Taking a look at your screen",
    terminal=True,
)
def read_screen(question: str = "") -> str:
    from app.services.vision import get_vision, VisionError
    try:
        return _run(get_vision().describe_screen(question))
    except VisionError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("read_screen failed")
        return f"I couldn't read your screen just now, sir ({type(e).__name__})."


@tool(
    "Look through the WEBCAM and identify what the boss is showing you — a physical object, a "
    "product, a label, something in the room. Use for 'what is this', 'what am I holding', 'look "
    "at this', 'can you see this'. For on-screen content use `read_screen` instead.",
    params={"question": {"type": "string",
                         "description": "his question about what he's showing (optional)"}},
    narration="Taking a look",
    terminal=True,
)
def look(question: str = "") -> str:
    from app.services.vision import get_vision, VisionError
    try:
        return _run(get_vision().look(question))
    except VisionError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("look failed")
        return f"I couldn't get a look through the camera just now, sir ({type(e).__name__})."


@tool(
    "Describe or read a local IMAGE FILE on the PC (jpg/png/webp). Use for 'describe the image at "
    "<path>', 'what's in this photo <path>', 'read the text in <path>'.",
    params={
        "path": {"type": "string", "description": "full path to the image file on this PC"},
        "question": {"type": "string", "description": "optional specific question about it"},
    },
    required=["path"],
    narration="Looking at that image",
    terminal=True,
)
def describe_image(path: str, question: str = "") -> str:
    from app.services.vision import get_vision, VisionError
    try:
        return _run(get_vision().describe_image_file(path, question))
    except VisionError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("describe_image failed")
        return f"I couldn't open that image just now, sir ({type(e).__name__})."
