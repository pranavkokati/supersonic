"""OpenAI provider — direct REST call against the Chat Completions API."""

from __future__ import annotations

import logging
from typing import List, Optional

import httpx

from supersonic.config import UserSecrets
from supersonic.providers.base import CompletionResult, LLMProvider, Message, ProviderError

logger = logging.getLogger(__name__)

_API_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIProvider(LLMProvider):
    name = "openai"
    default_model = "gpt-4.1"
    fast_model = "gpt-4.1-mini"

    def __init__(self, secrets: UserSecrets):
        self.api_key = (secrets.openai_api_key or "").strip()

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
            raise ProviderError("OPENAI_API_KEY not configured")

        payload = {
            "model": model or self.default_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Authorization": f"Bearer {self.api_key}", "content-type": "application/json"}

        try:
            resp = httpx.post(_API_URL, headers=headers, json=payload, timeout=120.0)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise ProviderError(f"OpenAI API error {e.response.status_code}: {e.response.text[:400]}") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"OpenAI request failed: {e}") from e

        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        text = choice.get("message", {}).get("content", "")
        usage = data.get("usage", {})
        return CompletionResult(
            text=text,
            provider=self.name,
            model=data.get("model", model or self.default_model),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            raw=data,
        )
