"""
Free provider catalog for the KeyRotator.

Every chat/vision/embed provider here speaks the OpenAI-compatible HTTP API (Groq, Gemini,
OpenRouter, Together, Cerebras, Mistral all do), so one adapter drives them all — only the
base URL, model name, and key change. STT providers (Groq Whisper, Deepgram) are listed
separately since transcription has its own shape.

A provider is ACTIVE only if at least one key is present in .env. Empty slots
(Together/Cerebras/Mistral until you add a key) are simply skipped — drop a key in and the
provider lights up with no code change.

`daily_limit` / `rpm` are the free-tier ceilings we track against per key (conservative;
better to rotate early than to 429).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config import load_key_pool, GROQ_WHISPER_MODEL, DEEPGRAM_STT_MODEL


# --------------------------------------------------------------------------- #
# Chat / vision / embed — OpenAI-compatible
# --------------------------------------------------------------------------- #
@dataclass
class ProviderSpec:
    name: str
    key_prefix: str                 # env base name, e.g. "GROQ_API_KEY"
    base_url: str                   # OpenAI-compatible /chat/completions root
    priority: int                   # lower = preferred
    tasks: set[str]                 # subset of {"chat","vision","embed"}
    chat_model: str | None = None
    vision_model: str | None = None
    embed_model: str | None = None
    daily_limit: int = 1000         # free req/day per key (tracked)
    rpm: int = 30                   # req/min per key (tracked)
    extra_headers: dict = field(default_factory=dict)
    tool_fallbacks: list[str] = field(default_factory=list)  # models to retry on malformed tool call
    keys: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.keys)

    def model_for(self, task: str) -> str | None:
        return {"chat": self.chat_model, "vision": self.vision_model, "embed": self.embed_model}.get(task)


def _spec(**kw) -> ProviderSpec:
    s = ProviderSpec(**kw)
    s.keys = load_key_pool(s.key_prefix)
    return s


# Catalog. Order here is the default preference (also enforced by `priority`).
_CHAT_CATALOG = [
    _spec(
        name="groq", key_prefix="GROQ_API_KEY",
        base_url="https://api.groq.com/openai/v1", priority=0,
        tasks={"chat", "vision"},
        chat_model="llama-3.3-70b-versatile",
        vision_model="meta-llama/llama-4-scout-17b-16e-instruct",
        daily_limit=14000, rpm=28,
        tool_fallbacks=["meta-llama/llama-4-scout-17b-16e-instruct", "llama-3.1-8b-instant"],
    ),
    _spec(
        name="cerebras", key_prefix="CEREBRAS_API_KEY",
        base_url="https://api.cerebras.ai/v1", priority=1,
        tasks={"chat"},
        chat_model="gpt-oss-120b",
        daily_limit=14000, rpm=28,
        tool_fallbacks=["zai-glm-4.7"],
    ),
    _spec(
        name="gemini", key_prefix="GEMINI_API_KEY",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        priority=2, tasks={"chat", "vision", "embed"},
        chat_model="gemini-2.0-flash",
        vision_model="gemini-2.5-flash",
        embed_model="text-embedding-004",
        daily_limit=1400, rpm=14,
    ),
    _spec(
        name="together", key_prefix="TOGETHER_API_KEY",
        base_url="https://api.together.xyz/v1", priority=3,
        tasks={"chat", "vision"},
        chat_model="meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
        vision_model="meta-llama/Llama-Vision-Free",
        daily_limit=1000, rpm=20,
    ),
    _spec(
        name="mistral", key_prefix="MISTRAL_API_KEY",
        base_url="https://api.mistral.ai/v1", priority=4,
        tasks={"chat", "embed"},
        chat_model="mistral-small-latest",
        embed_model="mistral-embed",
        daily_limit=1000, rpm=20,
    ),
    _spec(
        name="openrouter", key_prefix="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1", priority=5,
        tasks={"chat", "vision"},
        chat_model="meta-llama/llama-3.3-70b-instruct:free",
        vision_model="meta-llama/llama-3.2-11b-vision-instruct:free",
        daily_limit=200, rpm=20,
        extra_headers={"HTTP-Referer": "https://localhost", "X-Title": "JARVIS"},
    ),
]


# --------------------------------------------------------------------------- #
# STT providers (transcription has its own request shape)
# --------------------------------------------------------------------------- #
@dataclass
class STTSpec:
    name: str
    key_prefix: str
    priority: int
    model: str
    daily_limit: int = 2000
    keys: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.keys)


def _stt(**kw) -> STTSpec:
    s = STTSpec(**kw)
    s.keys = load_key_pool(s.key_prefix)
    return s


_STT_CATALOG = [
    # Deepgram Nova FIRST — it transcribes fast, natural, run-together speech far more accurately
    # than Whisper (which drops/merges words when you speak quickly). Groq Whisper stays as the
    # fallback if every Deepgram key is busy, so JARVIS always has an ear.
    _stt(name="deepgram", key_prefix="DEEPGRAM_API_KEY", priority=0,
         model=DEEPGRAM_STT_MODEL, daily_limit=20000),
    _stt(name="groq", key_prefix="GROQ_API_KEY", priority=1,
         model=GROQ_WHISPER_MODEL, daily_limit=7000),
]


def active_chat_providers() -> list[ProviderSpec]:
    return sorted([p for p in _CHAT_CATALOG if p.active], key=lambda p: p.priority)


def active_stt_providers() -> list[STTSpec]:
    return sorted([p for p in _STT_CATALOG if p.active], key=lambda p: p.priority)


def all_specs() -> list[ProviderSpec]:
    """Including inactive (for /admin/key-stats visibility)."""
    return _CHAT_CATALOG
