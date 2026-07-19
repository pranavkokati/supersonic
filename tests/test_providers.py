"""Provider abstraction — auto-detection and JSON parsing."""

from __future__ import annotations

import pytest

from supersonic.config import UserSecrets
from supersonic.providers import available_providers, get_provider
from supersonic.providers.anthropic_provider import AnthropicProvider
from supersonic.providers.base import ProviderError, parse_json_loose
from supersonic.providers.ollama_provider import OllamaProvider
from supersonic.providers.openai_provider import OpenAIProvider


def test_anthropic_unavailable_without_key():
    provider = AnthropicProvider(UserSecrets(anthropic_api_key=""))
    assert provider.available() is False


def test_anthropic_available_with_key():
    provider = AnthropicProvider(UserSecrets(anthropic_api_key="sk-test-123"))
    assert provider.available() is True


def test_openai_unavailable_without_key():
    provider = OpenAIProvider(UserSecrets(openai_api_key=""))
    assert provider.available() is False


def test_ollama_unavailable_when_unreachable():
    # Port 1 is reserved/unroutable — this should fail fast, not hang.
    provider = OllamaProvider(UserSecrets(ollama_base_url="http://127.0.0.1:1"))
    assert provider.available() is False


def test_get_provider_raises_when_nothing_configured():
    secrets = UserSecrets(anthropic_api_key="", openai_api_key="", ollama_base_url="http://127.0.0.1:1")
    with pytest.raises(ProviderError):
        get_provider(secrets)


def test_get_provider_picks_anthropic_first_when_multiple_configured():
    secrets = UserSecrets(anthropic_api_key="sk-a", openai_api_key="sk-b")
    provider = get_provider(secrets)
    assert provider.name == "anthropic"


def test_get_provider_honors_prefer():
    secrets = UserSecrets(anthropic_api_key="sk-a", openai_api_key="sk-b")
    provider = get_provider(secrets, prefer="openai")
    assert provider.name == "openai"


def test_available_providers_lists_configured_only():
    secrets = UserSecrets(anthropic_api_key="sk-a", openai_api_key="", ollama_base_url="http://127.0.0.1:1")
    names = available_providers(secrets)
    assert "anthropic" in names
    assert "openai" not in names
    assert "ollama" not in names


def test_parse_json_loose_handles_markdown_fence():
    text = '```json\n{"done": true, "reason": "complete"}\n```'
    parsed = parse_json_loose(text)
    assert parsed == {"done": True, "reason": "complete"}


def test_parse_json_loose_handles_surrounding_prose():
    text = 'Sure, here is the plan:\n{"follow_up": "add tests"}\nLet me know if you need changes.'
    parsed = parse_json_loose(text)
    assert parsed["follow_up"] == "add tests"


def test_parse_json_loose_raises_on_no_json():
    with pytest.raises(ProviderError):
        parse_json_loose("no json here at all")
