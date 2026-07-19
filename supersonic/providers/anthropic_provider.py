"""Anthropic Claude provider — direct REST call, no SDK dependency required."""

from __future__ import annotations

import logging
from typing import List, Optional

import httpx

from supersonic.config import UserSecrets
from supersonic.providers.base import CompletionResult, LLMProvider, Message, ProviderError

logger = logging.getLogger(__name__)

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    default_model = "claude-sonnet-5"
    fast_model = "claude-haiku-4-5-20251001"

    def __init__(self, secrets: UserSecrets):
        self.api_key = (secrets.anthropic_api_key or "").strip()

    def available(self) -> bool:
        return bool(self.api_key)

    def complete(
        self,
        messages: List[Message],
        *,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.4,
        json_mode: bool = False,
    ) -> CompletionResult:
        if not self.available():
            raise ProviderError("ANTHROPIC_API_KEY not configured")

        system = "\n\n".join(m.content for m in messages if m.role == "system")
        turns = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
        if json_mode:
            system = (
                system + "\n\nRespond with a single valid JSON object only. No prose, no markdown fences."
            ).strip()

        payload = {
            "model": model or self.default_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": turns,
        }
        if system:
            payload["system"] = system

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

        try:
            resp = httpx.post(_API_URL, headers=headers, json=payload, timeout=120.0)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise ProviderError(f"Anthropic API error {e.response.status_code}: {e.response.text[:400]}") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"Anthropic request failed: {e}") from e

        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage", {})
        return CompletionResult(
            text=text,
            provider=self.name,
            model=data.get("model", model or self.default_model),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            raw=data,
        )
