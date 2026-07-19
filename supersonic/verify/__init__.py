"""Verification suite — four independent signals gated into one pass/fail decision.

qa.py      tests + lint/typecheck (auto-detected, degrade to not-run cleanly)
critic.py  LLM judge of intent-match against the turn's goal + ledger invariants
thrash.py  diff-similarity oscillation detector across recent turns
gate.py    combines the above into a single GateResult the loop acts on
"""

from supersonic.verify.gate import GateResult, run_gate

__all__ = ["GateResult", "run_gate"]
