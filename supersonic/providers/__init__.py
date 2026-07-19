"""Provider auto-detection — the whole system runs on whichever LLM key is present.

Priority: Anthropic > OpenAI > Ollama (local, free, always tried last since it
requires no key at all). `preferred_provider` in config overrides the order.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from supersonic.config import UserSecrets
from supersonic.providers.base import CompletionResult, LLMProvider, Message, ProviderError, parse_json_loose

logger = logging.getLogger(__name__)

__all__ = [
    "CompletionResult",
    "LLMProvider",
    "Message",
    "ProviderError",
    "parse_json_loose",
    "get_provider",
    "available_providers",
]


def _candidates():
    from supersonic.providers.anthropic_provider import AnthropicProvider
    from supersonic.providers.ollama_provider import OllamaProvider
    from supersonic.providers.openai_provider import OpenAIProvider

    return [AnthropicProvider, OpenAIProvider, OllamaProvider]


def available_providers(secrets: UserSecrets) -> List[str]:
    names = []
    for cls in _candidates():
        try:
            if cls(secrets).available():
                names.append(cls.name)
        except Exception:  # pragma: no cover - defensive, provider probing must never crash callers
            logger.exception("Provider probe failed for %s", cls.name)
    return names


def get_provider(secrets: UserSecrets, prefer: Optional[str] = None) -> LLMProvider:
    """Return the first configured provider, honoring `prefer` (falls through if unavailable)."""
    prefer = prefer or secrets.preferred_provider or None
    candidates = _candidates()
    if prefer:
        candidates = sorted(candidates, key=lambda c: 0 if c.name == prefer else 1)

    tried = []
    for cls in candidates:
        provider = cls(secrets)
        tried.append(provider.name)
        if provider.available():
            logger.info("Using LLM provider: %s (model=%s)", provider.name, provider.default_model)
            return provider

    raise ProviderError(
        "No LLM provider configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY, or run a local "
        f"`ollama serve`. Tried in order: {', '.join(tried)}."
    )


def get_provider_by_name(name: str, secrets: UserSecrets) -> LLMProvider:
    for cls in _candidates():
        if cls.name == name:
            return cls(secrets)
    raise ProviderError(f"Unknown provider: {name}")
