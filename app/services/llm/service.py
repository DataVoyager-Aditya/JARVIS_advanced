"""
LLMService — JARVIS's text brain.

Same public API the voice loop has always used (`chat_stream`, `chat`), now backed by the
multi-provider KeyRotator instead of Groq-only. The voice router and listener don't change.
Builds the JARVIS system prompt + history + user turn into OpenAI-style messages and hands
off to the rotator, which picks the best free provider/key with quota remaining.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Iterable

from config import build_system_prompt
from app.services.llm.key_rotator import get_rotator

logger = logging.getLogger("jarvis.llm")


class LLMService:
    def __init__(self) -> None:
        self.rotator = get_rotator()
        if not self.rotator.chat_providers:
            raise RuntimeError(
                "No chat providers active — add at least one free key "
                "(GROQ_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY ...) to .env."
            )

    def _messages(self, user_text: str, history: Iterable[dict] | None) -> list[dict]:
        msgs: list[dict] = [{"role": "system", "content": build_system_prompt()}]
        if history:
            msgs.extend(history)
        msgs.append({"role": "user", "content": user_text})
        return msgs

    async def chat_stream(
        self, user_text: str, history: list[dict] | None = None, temperature: float = 0.5,
    ) -> AsyncIterator[str]:
        async for delta in self.rotator.chat_stream(
            self._messages(user_text, history), task="chat", temperature=temperature
        ):
            yield delta

    async def chat(
        self, user_text: str, history: list[dict] | None = None, temperature: float = 0.5,
    ) -> str:
        return await self.rotator.chat(
            self._messages(user_text, history), task="chat", temperature=temperature
        )


_singleton: LLMService | None = None


def get_llm() -> LLMService:
    global _singleton
    if _singleton is None:
        _singleton = LLMService()
    return _singleton
