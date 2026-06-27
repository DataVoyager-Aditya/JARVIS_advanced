"""Multi-provider LLM layer — the generalized 'Groq trick' (Phase 3 KeyRotator)."""

from app.services.llm.service import LLMService, get_llm
from app.services.llm.key_rotator import KeyRotator, get_rotator

__all__ = ["LLMService", "get_llm", "KeyRotator", "get_rotator"]
