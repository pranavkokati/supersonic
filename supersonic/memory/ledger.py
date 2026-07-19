"""Continuity Graph ledger — an append-only, git-committed store of everything
the build loop has learned about a project: decisions, invariants, failures,
and distilled lessons.

This is what replaces token-level KV eviction ("SuperCompress" in the prior
generation of this tool). Instead of compressing a flat transcript down to
fit a budget, Supersonic writes structured facts as they're discovered and
retrieves only what's relevant to the current turn (see graph.py). Nothing is
silently dropped — old entries get distilled into lesson nodes, never
deleted, so the ledger doubles as a durable audit trail.

Stored at <workdir>/.continuity/ledger.jsonl — plain JSONL, human-diffable,
committed to the project's own git history alongside the code it describes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, List

from supersonic.memory.schema import EntryKind, LedgerEntry, new_entry

logger = logging.getLogger(__name__)

LEDGER_DIRNAME = ".continuity"
LEDGER_FILENAME = "ledger.jsonl"
BRAIN_FILENAME = "BRAIN.md"


class ContinuityLedger:
    """Append-only JSONL store scoped to one project workdir."""

    def __init__(self, workdir: Path):
        self.workdir = Path(workdir)
        self.dir = self.workdir / LEDGER_DIRNAME
        self.path = self.dir / LEDGER_FILENAME
        self.brain_path = self.dir / BRAIN_FILENAME
        self.dir.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    # -- writes ------------------------------------------------------------

    def append(self, entry: LedgerEntry) -> LedgerEntry:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        logger.debug("ledger += %s %s: %s", entry.kind, entry.id, entry.title[:60])
        return entry

    def record_decision(self, turn: int, title: str, body: str, **kw) -> LedgerEntry:
        return self.append(new_entry("decision", turn, title, body, source=kw.pop("source", "loop"), **kw))

    def record_invariant(self, turn: int, title: str, body: str, **kw) -> LedgerEntry:
        return self.append(new_entry("invariant", turn, title, body, source=kw.pop("source", "loop"), **kw))

    def record_failure(self, turn: int, title: str, body: str, **kw) -> LedgerEntry:
        return self.append(new_entry("failure", turn, title, body, source=kw.pop("source", "verify"), **kw))

    def record_lesson(self, turn: int, title: str, body: str, **kw) -> LedgerEntry:
        return self.append(new_entry("lesson", turn, title, body, source=kw.pop("source", "loop"), **kw))

    def record_fact(self, turn: int, title: str, body: str, **kw) -> LedgerEntry:
        return self.append(new_entry("fact", turn, title, body, source=kw.pop("source", "agent"), **kw))

    def supersede(self, old_id: str, new: LedgerEntry) -> LedgerEntry:
        """Flag an old entry as superseded, then append the replacement."""
        entries = self.all()
        for e in entries:
            if e.id == old_id:
                e.superseded_by = new.id
        self.replace_all(entries)
        return self.append(new)

    def replace_all(self, entries: Iterable[LedgerEntry]) -> None:
        with self.path.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")

    # -- reads ---------------------------------------------------------------

    def all(self, include_superseded: bool = True) -> List[LedgerEntry]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(LedgerEntry.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                logger.warning("skipping malformed ledger line")
                continue
        if not include_superseded:
            out = [e for e in out if not e.superseded_by]
        return out

    def by_kind(self, kind: EntryKind, include_superseded: bool = False) -> List[LedgerEntry]:
        return [e for e in self.all(include_superseded) if e.kind == kind]

    def invariants(self) -> List[LedgerEntry]:
        return self.by_kind("invariant")

    def open_failures(self) -> List[LedgerEntry]:
        return self.by_kind("failure")

    def recent(self, n: int = 10) -> List[LedgerEntry]:
        return self.all(include_superseded=False)[-n:]

    def stats(self) -> dict:
        entries = self.all(include_superseded=False)
        counts: dict = {}
        for e in entries:
            counts[e.kind] = counts.get(e.kind, 0) + 1
        return {"total": len(entries), "by_kind": counts}

    # -- human/agent-readable snapshot -----------------------------------

    def render_brain(self) -> str:
        """Write a plain-markdown snapshot any coding agent can read directly
        off disk, even one that never calls into our retrieval API."""
        entries = self.all(include_superseded=False)
        inv = [e for e in entries if e.kind == "invariant"]
        fail = [e for e in entries if e.kind == "failure"]
        lessons = [e for e in entries if e.kind == "lesson"]
        decisions = [e for e in entries if e.kind == "decision"][-10:]

        lines = ["# Project memory (Continuity Graph)", "", f"_{len(entries)} entries · auto-generated, do not hand-edit._", ""]
        if inv:
            lines.append("## Invariants — never violate these")
            lines += [f"- **{e.title}** — {e.body}" for e in inv]
            lines.append("")
        if fail:
            lines.append("## Known failure modes — do not repeat")
            lines += [f"- (turn {e.turn}) **{e.title}** — {e.body}" for e in fail]
            lines.append("")
        if lessons:
            lines.append("## Distilled lessons")
            lines += [f"- {e.title} — {e.body}" for e in lessons]
            lines.append("")
        if decisions:
            lines.append("## Recent decisions")
            lines += [f"- (turn {e.turn}) {e.title} — {e.body}" for e in decisions]
            lines.append("")
        text = "\n".join(lines)
        self.brain_path.write_text(text, encoding="utf-8")
        return text
