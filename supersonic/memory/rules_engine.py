"""Self-Evolving Rules Engine — the Continuity Graph learns durable rules,
not just a retrieved-context ledger.

Honest scope note: this is not genetic-algorithm prompt mutation across a
population of candidate prompts scored against a fitness function — that's
what OpenEvolve and EvoAgentX actually do (evolutionary search over many
variants). This is a narrower, simpler mechanism: when the SAME category of
Verify failure repeats across turns, one supervisor-critic LLM call
synthesizes ONE concise, durable rule from the specific failure trace that
just happened, and it's appended — never silently rewritten or randomly
mutated — to this project's own rules file. That file is folded into every
subsequent turn's prompt, and, best-effort, mirrored into whichever
already-existing real project convention file the user's own repo has
(`.cursorrules` for Cursor, `CLAUDE.md` for Claude Code) — never created
from scratch, since that would be silently opting a project into a tool
convention it never asked for.

Persistence: `.supersonic/rules.json` (structured, one entry per rule) and
`.supersonic/rules.md` (rendered, human/agent-readable) inside the project
workdir — same pattern as the Continuity Ledger's `.continuity/ledger.jsonl`
+ `BRAIN.md` and the receipts/dependency-mapper caches under `.supersonic/`
and `.dle/`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from supersonic.memory.ledger import ContinuityLedger
from supersonic.providers.base import LLMProvider, Message

logger = logging.getLogger(__name__)

RULES_DIRNAME = ".supersonic"
RULES_FILENAME = "rules.json"
RULES_MD_FILENAME = "rules.md"

_MIRROR_START = "<!-- supersonic:auto-rules:start -->"
_MIRROR_END = "<!-- supersonic:auto-rules:end -->"

# Every gate short-circuit / signal failure this engine is willing to learn
# from, and the plain-English label used in the rendered rules file.
_CATEGORY_LABELS = {
    "syntax_shield": "Syntax Shield (broken Python/JS syntax)",
    "dependency_trust": "Dependency Trust Gate (hallucinated package)",
    "secret_leak": "Secret Leak Gate (hardcoded credential)",
    "tests": "failing test suite",
    "lint": "lint/typecheck failure",
    "critic": "goal-satisfaction critic",
    "thrash": "thrash detector (repeating the same failed change)",
    "test_quality": "Test Quality Gate (weak test coverage)",
}


def classify_gate_failure(gate) -> str:
    """Map a failed GateResult to one of the categories above. `gate` is
    typed loosely (verify.gate.GateResult) to avoid a circular import."""
    if gate.dependency_trust.ran and not gate.dependency_trust.ok:
        return "dependency_trust"
    if gate.secret_leak.ran and not gate.secret_leak.ok:
        return "secret_leak"
    if "Syntax Shield" in gate.summary:
        return "syntax_shield"
    if gate.tests.ran and not gate.tests.passed:
        return "tests"
    if gate.lint.ran and not gate.lint.passed:
        return "lint"
    if gate.test_quality.ran and not gate.test_quality.passed:
        return "test_quality"
    if gate.critic.ran and not gate.critic.satisfied:
        return "critic"
    if gate.thrash.ran and gate.thrash.thrashing:
        return "thrash"
    return "unknown"


@dataclass
class Rule:
    id: str
    category: str
    rule_text: str
    source_turn: int
    source_failure: str
    repeats_observed: int
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        known = {k: d.get(k) for k in cls.__dataclass_fields__.keys() if k in d}
        return cls(**known)


class RulesStore:
    """Owns `.supersonic/rules.json` + the rendered `.supersonic/rules.md` for one workdir."""

    def __init__(self, workdir: Path):
        self.workdir = Path(workdir)
        self.dir = self.workdir / RULES_DIRNAME
        self.path = self.dir / RULES_FILENAME
        self.md_path = self.dir / RULES_MD_FILENAME
        self.dir.mkdir(parents=True, exist_ok=True)

    def all(self) -> List[Rule]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        except (json.JSONDecodeError, OSError):
            logger.warning("rules.json unreadable, treating as empty")
            return []
        return [Rule.from_dict(r) for r in raw if isinstance(r, dict)]

    def has_rule_for_category(self, category: str) -> bool:
        return any(r.category == category for r in self.all())

    def add(self, rule: Rule) -> None:
        rules = self.all()
        rules.append(rule)
        self.path.write_text(
            json.dumps([r.to_dict() for r in rules], indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._render_md(rules)
        self._mirror_to_existing_convention_files(rules)

    def _render_md(self, rules: List[Rule]) -> str:
        lines = [
            "# Rules learned from this project (Supersonic)",
            "",
            f"_{len(rules)} rule(s), each synthesized after a Verify failure category repeated. "
            "Auto-generated, do not hand-edit._",
            "",
        ]
        for r in rules:
            label = _CATEGORY_LABELS.get(r.category, r.category)
            lines.append(f"- **{label}** (turn {r.source_turn}, seen {r.repeats_observed}x): {r.rule_text}")
        text = "\n".join(lines) + "\n"
        self.md_path.write_text(text, encoding="utf-8")
        return text

    def active_rules_block(self) -> str:
        rules = self.all()
        if not rules:
            return ""
        lines = ["## Rules learned from this project (do not repeat these mistakes)"]
        for r in rules:
            lines.append(f"- {r.rule_text}")
        return "\n".join(lines)

    def _mirror_to_existing_convention_files(self, rules: List[Rule]) -> None:
        """Best-effort: only touches a convention file the project already
        has (never creates one), and only ever rewrites the clearly marked
        auto-generated section — anything the user wrote above or below it
        is left untouched."""
        block = "\n".join(f"- {r.rule_text}" for r in rules)
        section = f"{_MIRROR_START}\n## Rules learned by Supersonic\n{block}\n{_MIRROR_END}"
        for filename in (".cursorrules", "CLAUDE.md"):
            target = self.workdir / filename
            if not target.exists():
                continue
            try:
                self._update_marked_section(target, section)
            except OSError:
                logger.exception("failed to mirror rules into %s", filename)

    @staticmethod
    def _update_marked_section(target: Path, section: str) -> None:
        text = target.read_text(encoding="utf-8")
        if _MIRROR_START in text and _MIRROR_END in text:
            before = text.split(_MIRROR_START)[0].rstrip()
            after = text.split(_MIRROR_END)[1].lstrip()
            new_text = f"{before}\n\n{section}\n\n{after}" if after else f"{before}\n\n{section}\n"
        else:
            new_text = f"{text.rstrip()}\n\n{section}\n"
        target.write_text(new_text, encoding="utf-8")


def _repeats_for_category(ledger: ContinuityLedger, category: str) -> int:
    return sum(1 for e in ledger.by_kind("failure", include_superseded=True) if category in e.tags)


def observe_failure(
    workdir: Path,
    ledger: ContinuityLedger,
    *,
    gate,
    turn: int,
    failure_title: str,
    failure_body: str,
    provider: Optional[LLMProvider],
    min_repeats: int = 2,
) -> Optional[Rule]:
    """Call once per failed turn, after `ledger.record_failure(...)` has
    already tagged the entry with the failure category (see orchestrator.py).
    Returns the newly synthesized Rule if one was created this call, else
    None (either the repeat threshold isn't met yet, a rule for this
    category already exists, or no provider is available to write one)."""
    category = classify_gate_failure(gate)
    if category == "unknown":
        return None

    store = RulesStore(workdir)
    if store.has_rule_for_category(category):
        return None  # one rule per category is enough — don't spam the prompt

    repeats = _repeats_for_category(ledger, category)
    if repeats < min_repeats:
        return None

    if provider is None:
        return None

    label = _CATEGORY_LABELS.get(category, category)
    prompt = (
        f"A coding agent building a project just failed the same Verify check "
        f"({label}) for the {repeats}th time. Latest failure:\n\n"
        f"{failure_title}\n{failure_body[:1200]}\n\n"
        "Write exactly ONE concise, durable, imperative rule (under 200 characters) "
        "for this agent's future instructions that would have prevented this specific "
        "class of failure. Return a single JSON object: {\"rule\": \"...\"}"
    )
    try:
        parsed = provider.complete_json([Message(role="user", content=prompt)], max_tokens=200)
        rule_text = str(parsed.get("rule", "")).strip()
    except Exception:  # noqa: BLE001 - a rule-synthesis failure must never break the loop
        logger.exception("rules engine: failed to synthesize a rule, continuing without one")
        return None

    if not rule_text:
        return None
    rule_text = rule_text[:280]

    rule = Rule(
        id=f"{category}-{turn}",
        category=category,
        rule_text=rule_text,
        source_turn=turn,
        source_failure=failure_title[:200],
        repeats_observed=repeats,
    )
    store.add(rule)
    return rule


def active_rules_block(workdir: Path) -> str:
    """What `_build_prompt` folds into every subsequent turn's prompt."""
    return RulesStore(workdir).active_rules_block()
