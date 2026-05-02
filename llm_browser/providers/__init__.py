"""Provider registry — maps names/aliases to provider instances."""

from llm_browser.providers.base import BaseProvider
from llm_browser.providers.claude import ClaudeProvider
from llm_browser.providers.chatgpt import ChatGPTProvider
from llm_browser.providers.gemini import GeminiProvider

_PROVIDERS: dict[str, BaseProvider] = {
    p.meta.name: p  # type: ignore[attr-defined]
    for p in [ClaudeProvider(), ChatGPTProvider(), GeminiProvider()]
}

_ALIASES: dict[str, str] = {
    "c": "claude",
    "gpt": "chatgpt",
    "openai": "chatgpt",
    "g": "gemini",
    "bard": "gemini",
}


def get_provider(name: str) -> BaseProvider:
    key = _ALIASES.get(name.lower(), name.lower())
    if key not in _PROVIDERS:
        available = ", ".join(_PROVIDERS)
        raise ValueError(f"Unknown provider '{name}'. Available: {available}")
    return _PROVIDERS[key]


def list_providers() -> list[BaseProvider]:
    return list(_PROVIDERS.values())