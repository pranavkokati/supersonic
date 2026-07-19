"""Provider abstraction — the common interface every LLM backend implements.

Supersonic never hardcodes a vendor into the planner, critic, or bandit
classifier. Every LLM call in the system goes through this interface so the
whole product works with whichever key the user actually has.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class CompletionResult:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    raw: Any = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ProviderError(RuntimeError):
    """Raised when a provider call fails, is misconfigured, or returns unusable output."""


class LLMProvider(ABC):
    """One vendor's implementation of chat completion, used interchangeably everywhere."""

    name: str = "base"
    default_model: str = ""
    fast_model: str = ""  # cheap/low-latency model for high-frequency calls (critic, bandit classifier)

    @abstractmethod
    def complete(
        self,
        messages: List[Message],
        *,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.4,
        json_mode: bool = False,
    ) -> CompletionResult:
        ...

    def complete_json(
        self,
        messages: List[Message],
        *,
        model: Optional[str] = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> Dict[str, Any]:
        result = self.complete(
            messages, model=model, max_tokens=max_tokens, temperature=temperature, json_mode=True
        )
        return parse_json_loose(result.text)

    def fast(
        self,
        messages: List[Message],
        *,
        max_tokens: int = 512,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> CompletionResult:
        """Route to the cheap/low-latency model — used for the bandit classifier and thrash checks."""
        return self.complete(
            messages,
            model=self.fast_model or self.default_model,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=json_mode,
        )

    def available(self) -> bool:
        return True


def parse_json_loose(text: str) -> Dict[str, Any]:
    """Extract a JSON object from model output, tolerating markdown fences and stray prose."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ProviderError(f"No JSON object found in model output: {text[:200]!r}")
    snippet = text[start : end + 1]
    try:
        return json.loads(snippet)
    except json.JSONDecodeError as e:
        raise ProviderError(f"Malformed JSON from model: {e}\n{snippet[:400]}") from e
