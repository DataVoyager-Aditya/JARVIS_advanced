"""
Shared streaming helpers for the voice pipeline.

`sentence_chunks` turns the LLM's token stream into complete spoken sentences as soon as
each one is ready — so TTS on sentence 1 starts while the LLM is still writing sentence 2.
This is what makes JARVIS start talking almost immediately instead of after the whole reply.
"""

from __future__ import annotations

import re
from typing import AsyncIterator

# End a sentence on . ! ? … or a newline, when followed by whitespace/end.
_BOUNDARY = re.compile(r"(.+?[.!?…\n]+)(\s|$)", re.DOTALL)
# Don't emit a 1-3 char fragment as its own sentence (e.g. "Mr."): require some length,
# unless the buffer is clearly getting long.
_MIN_LEN = 12
_MAX_LEN = 240


async def sentence_chunks(text_stream: AsyncIterator[str]) -> AsyncIterator[str]:
    buf = ""
    async for delta in text_stream:
        buf += delta
        while True:
            m = _BOUNDARY.match(buf)
            if m and (len(m.group(1).strip()) >= _MIN_LEN or len(buf) >= _MAX_LEN):
                sentence = m.group(1).strip()
                buf = buf[m.end():]
                if sentence:
                    yield sentence
            elif len(buf) >= _MAX_LEN:
                # Hard flush very long run-ons at the last space.
                cut = buf.rfind(" ", 0, _MAX_LEN)
                cut = cut if cut > _MIN_LEN else _MAX_LEN
                chunk, buf = buf[:cut].strip(), buf[cut:]
                if chunk:
                    yield chunk
            else:
                break
    tail = buf.strip()
    if tail:
        yield tail
