"""Ollama provider — local, free, zero-API-key inference for anyone running a local model.

This is what makes Supersonic runnable with $0 in API spend: point it at a
local Ollama server and the planner and critic both work against a
self-hosted model.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import httpx

from supersonic.config import UserSecrets
from supersonic.providers.base import CompletionResult, LLMProvider, Message, ProviderError

logger = logging.getLogger(__name__)


class OllamaProvider(LLMProvider):
    name = "ollama"
    default_model = "llama3.1"
    fast_model = "llama3.1"

    def __init__(self, secrets: UserSecrets):
        self.base_url = (secrets.ollama_base_url or "http://localhost:11434").rstrip("/")
        self._checked_reachable: Optional[bool] = None

    def available(self) -> bool:
        if self._checked_reachable is not None:
            return self._checked_reachable
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=1.5)
            self._checked_reachable = resp.status_code == 200
        except httpx.HTTPError:
            self._checked_reachable = False
        return self._checked_reachable

    def complete(
        self,
        messages: List[Message],
        *,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.4,
        json_mode: bool = False,
    ) -> CompletionResult:
        payload = {
            "model": model or self.default_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"

        try:
            resp = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=300.0)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ProviderError(f"Ollama request failed (is `ollama serve` running?): {e}") from e

        data = resp.json()
        text = data.get("message", {}).get("content", "")
        return CompletionResult(
            text=text,
            provider=self.name,
            model=data.get("model", model or self.default_model),
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
            raw=data,
        )
