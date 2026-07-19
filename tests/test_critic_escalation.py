"""Risk-Aware Model Escalation's lever on Supersonic's own critic call —
verify.judge()'s optional `model=` override, and gate.run_gate()'s
`critic_model=` pass-through, without hitting a real provider."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from supersonic.providers.base import CompletionResult, LLMProvider, Message
from supersonic.verify.critic import judge
from supersonic.verify.gate import run_gate


class _FakeProvider(LLMProvider):
    name = "fake"
    default_model = "fake-default"
    fast_model = "fake-fast"

    def __init__(self):
        self.seen_models: List[Optional[str]] = []

    def complete(
        self, messages: List[Message], *, model: Optional[str] = None, max_tokens: int = 4096,
        temperature: float = 0.4, json_mode: bool = False,
    ) -> CompletionResult:
        self.seen_models.append(model)
        return CompletionResult(
            text='{"satisfied": true, "confidence": 0.9, "reasoning": "ok", "violated_invariants": []}',
            provider=self.name, model=model or self.default_model,
        )


def test_judge_uses_fast_model_by_default():
    provider = _FakeProvider()
    judge(provider, goal="add a button", diff="+ok", invariants=[])
    assert provider.seen_models == ["fake-fast"]


def test_judge_uses_override_model_when_escalated():
    provider = _FakeProvider()
    judge(provider, goal="add a button", diff="+ok", invariants=[], model="fake-escalated")
    assert provider.seen_models == ["fake-escalated"]


def test_run_gate_threads_critic_model_override_through(tmp_path: Path):
    provider = _FakeProvider()
    run_gate(
        tmp_path, provider=provider, goal="add a button", diff="+ok", invariants=[], recent_diffs=[],
        min_signals_pass=1, critic_model="fake-escalated",
    )
    assert provider.seen_models == ["fake-escalated"]


def test_run_gate_omits_critic_model_by_default(tmp_path: Path):
    provider = _FakeProvider()
    run_gate(
        tmp_path, provider=provider, goal="add a button", diff="+ok", invariants=[], recent_diffs=[],
        min_signals_pass=1,
    )
    assert provider.seen_models == ["fake-fast"]
