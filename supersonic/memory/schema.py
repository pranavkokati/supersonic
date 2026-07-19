"""Continuity Graph schema — the structured facts Supersonic remembers.

Four kinds of entry, each with a distinct role in the loop:
  - invariant : a constraint that must never be violated (always retrieved)
  - failure   : something that broke, with root cause (always retrieved,
                written automatically by the Verify/Rollback stage)
  - decision  : a choice made and why (relevance-ranked, distilled over time)
  - lesson    : a distilled summary of older decisions/facts
  - fact      : a plain observation worth remembering (e.g. "auth uses JWT")
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import List, Literal, Optional

EntryKind = Literal["decision", "invariant", "failure", "lesson", "fact"]

_DEFAULT_IMPORTANCE = {
    "invariant": 1.0,
    "failure": 0.9,
    "decision": 0.6,
    "lesson": 0.55,
    "fact": 0.4,
}


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LedgerEntry:
    id: str
    kind: EntryKind
    turn: int
    ts: str
    title: str
    body: str
    tags: List[str] = field(default_factory=list)
    refs: List[str] = field(default_factory=list)
    importance: float = 0.5
    superseded_by: Optional[str] = None
    source: str = "loop"  # "loop" | "agent" | "verify" | "user"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LedgerEntry":
        known = {k: d.get(k) for k in cls.__dataclass_fields__.keys() if k in d}
        known.setdefault("tags", [])
        known.setdefault("refs", [])
        return cls(**known)


def new_entry(
    kind: EntryKind,
    turn: int,
    title: str,
    body: str,
    *,
    tags: Optional[List[str]] = None,
    refs: Optional[List[str]] = None,
    importance: Optional[float] = None,
    source: str = "loop",
) -> LedgerEntry:
    return LedgerEntry(
        id=_new_id(),
        kind=kind,
        turn=turn,
        ts=_now(),
        title=title.strip(),
        body=body.strip(),
        tags=tags or [],
        refs=refs or [],
        importance=importance if importance is not None else _DEFAULT_IMPORTANCE.get(kind, 0.5),
        source=source,
    )
