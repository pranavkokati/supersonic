"""Verification suite — independent signals gated into one pass/fail decision.

qa.py             tests + lint/typecheck (auto-detected, degrade to not-run cleanly)
critic.py         LLM judge of intent-match against the turn's goal + ledger invariants
thrash.py         diff-similarity oscillation detector across recent turns
syntax_shield.py  DLE: fast ast.parse/bracket-balance check, runs BEFORE the signals below
telemetry_gate.py DLE: OPTIONAL fifth signal — browser-based runtime check, auto-skipped
                  when not applicable/available
gate.py           combines tests/lint/critic/thrash (+ telemetry, if supplied) into a
                  single GateResult the loop acts on
"""

from supersonic.verify.gate import GateResult, run_gate

__all__ = ["GateResult", "run_gate"]
