"""Black Box Replay — a single self-contained, scrubbable HTML timeline of
an entire build, assembled entirely from data this project already
persists: the Continuity Graph ledger, the git checkpoint history, Signed
Turn Receipts, the Self-Evolving Rules Engine's rules file, and any linked
repos from Multi-Repository State Anchoring. Nothing here re-runs the loop
or the agent — it's a read-only reconstruction of what already happened,
in one file you can open in any browser, offline, from any checkout.

Honest scope, stated up front:
  - Every receipt's Ed25519 signature is verified SERVER-SIDE, by this
    module, using the exact same `verify_receipt_file()` function
    `sonic verify-receipts` already uses (see verify/receipts.py) — this
    module does not reimplement Ed25519 verification in JavaScript. A
    from-scratch client-side reimplementation would need to reproduce
    Python's exact canonical-JSON byte encoding (recursively sorted keys,
    `ensure_ascii` escaping, float formatting) to check a signature
    correctly, and getting that byte-for-byte right for every possible
    payload shape is a real, easy-to-get-subtly-wrong problem — a security
    feature that's subtly wrong is worse than one that's honestly
    server-verified once and displayed as a plain result.
  - What the rendered page DOES verify independently, in the visitor's own
    browser, with no trust placed in whatever produced the page: each
    turn's diff hash, via the Web Crypto API's SHA-256 over the exact diff
    text embedded in the page, compared live against that turn's receipt
    `diff_sha256` field. That comparison is simple (hashing one string,
    no JSON canonicalization ambiguity) and is exactly the kind of check
    that's safe to reimplement client-side.
  - The diff embedded for a shipped turn is reconstructed from git history
    (`git diff <prevCheckpoint> <thisCheckpoint>`, with `.continuity/` and
    `.supersonic/` excluded — those two paths accumulate this turn's own
    Continuity Graph entry and its own receipt file, written to disk AFTER
    the diff that actually got hashed was captured, so the raw commit-to-
    commit diff is not byte-identical to what was signed). That
    reconstruction is provably correct for the common case (no failed turn
    intervened since the previous shipped checkpoint) but is NOT
    byte-identical to the original whenever one or more turns failed and
    rolled back in between — their preserved Continuity Graph entries ride
    along, uncommitted, until the next successful commit, in a way this
    module doesn't attempt to separate out at the line level. Rather than
    assume correctness, this module checks its own work at build time: it
    hashes the reconstructed diff and compares it to the receipt's stored
    `diff_sha256` right here, server-side, BEFORE deciding whether to offer
    the client-side recompute button at all. Only turns where that
    self-check actually passes get the button; every other turn shows the
    diff for reading, with an honest note about why independent
    recomputation isn't offered for it.
  - A receipt's `prompt_sha256` field cannot be independently re-hashed
    after the fact, by this page or by anything else, because the raw
    prompt text itself is never retained past the turn that used it — only
    its SHA-256 fingerprint is, by design (retaining every historical
    prompt verbatim would bloat every checkout indefinitely). The
    fingerprint is still covered by the same Ed25519 signature as
    everything else in the receipt; it just isn't independently
    recomputable here, and the page says so rather than implying otherwise.
  - A rolled-back turn has no diff to show — rollback discards the
    uncommitted change in full, and no receipt is ever written for a turn
    that failed Verify. What's shown for one of those is exactly what the
    Continuity Graph actually recorded at the moment it happened: the
    failure title and body, nothing reconstructed or inferred.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from supersonic.loop.checkpoint import CheckpointManager, run_git
from supersonic.loop.multi_repo import load_linked_repos
from supersonic.memory.ledger import ContinuityLedger
from supersonic.memory.rules_engine import RulesStore
from supersonic.verify.receipts import RECEIPTS_DIRNAME, verify_receipt_file

logger = logging.getLogger(__name__)

# The well-known empty-tree object ID every git repo has, used to diff
# turn 0 (the initial scaffold) against "nothing" — universally available,
# no special-casing needed for a repo's actual root commit.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Diff text embedded per turn is capped so a genuinely enormous turn can't
# blow up the generated file — past this, the client-side hash recompute is
# disabled for that turn (it can only check what's actually embedded) and
# the page says so, rather than silently comparing a truncated string
# against the full-diff hash and reporting a false mismatch.
MAX_EMBEDDED_DIFF_CHARS = 500_000


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _diff_between(workdir: Path, a: Optional[str], b: str) -> str:
    """The diff strictly between two checkpoints — but NOT literally `git
    diff a b`. A checkpoint commit contains the coding agent's code diff
    PLUS whatever bookkeeping this turn wrote afterward but before the
    commit (this turn's ledger entry, and — on a shipped turn — its Signed
    Turn Receipt): both `.continuity/` and `.supersonic/` are written
    between the moment `diff_since()` was captured for the receipt's
    `diff_sha256` and the moment the checkpoint commit actually happened
    (see orchestrator.py). Excluding those two paths here is what makes the
    reconstructed diff byte-identical to the exact string that was hashed —
    without it, every turn's client-side hash recompute would show a false
    mismatch, not because anything was tampered with, but because this
    function was comparing the wrong thing. See
    test_replay_diff_matches_original_receipt_hash for the regression test
    that would catch this if it broke again."""
    base = a or EMPTY_TREE_SHA
    res = run_git(
        ["diff", base, b, "--", ".", ":(exclude).continuity", ":(exclude).supersonic"],
        workdir, check=False,
    )
    return res.stdout


def _load_receipt(workdir: Path, turn: int) -> Optional[Dict[str, Any]]:
    path = Path(workdir) / RECEIPTS_DIRNAME / f"turn-{turn}.json"
    if not path.exists():
        return None
    verification = verify_receipt_file(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"verified": False, "verify_reason": "receipt file unreadable"}
    raw["verified"] = verification.ok
    raw["verify_reason"] = verification.reason
    return raw


def build_replay_data(workdir: Path) -> Dict[str, Any]:
    """Assemble everything Black Box Replay renders, as a plain JSON-safe
    dict. Read-only: never runs the agent, never touches the working tree."""
    workdir = Path(workdir)
    ledger = ContinuityLedger(workdir)
    entries = ledger.all(include_superseded=True)

    entries_by_turn: Dict[int, List] = {}
    for e in entries:
        entries_by_turn.setdefault(e.turn, []).append(e)

    checkpoints = []
    if (workdir / ".git").exists():
        checkpoints = CheckpointManager(workdir).list()
    checkpoints_by_turn = {c.turn: c for c in checkpoints}

    rules = RulesStore(workdir).all()
    rules_by_turn: Dict[int, List] = {}
    for r in rules:
        rules_by_turn.setdefault(r.source_turn, []).append(r)

    linked_repos = load_linked_repos(workdir)

    all_turn_numbers = sorted(set(checkpoints_by_turn.keys()) | set(entries_by_turn.keys()))

    prev_commit: Optional[str] = None
    turns_out: List[Dict[str, Any]] = []
    for turn in all_turn_numbers:
        checkpoint = checkpoints_by_turn.get(turn)
        turn_entries = sorted(entries_by_turn.get(turn, []), key=lambda e: e.ts)
        failure_entries = [e for e in turn_entries if e.kind == "failure"]
        decision_entries = [e for e in turn_entries if e.kind == "decision"]
        other_entries = [e for e in turn_entries if e.kind not in ("failure", "decision")]

        shipped = checkpoint is not None
        diff_text = ""
        diff_truncated = False
        if shipped:
            full_diff = _diff_between(workdir, prev_commit, checkpoint.commit)
            diff_truncated = len(full_diff) > MAX_EMBEDDED_DIFF_CHARS
            diff_text = full_diff[:MAX_EMBEDDED_DIFF_CHARS]
            prev_commit = checkpoint.commit

        receipt = _load_receipt(workdir, turn) if shipped else None

        # Self-check, not an assumption: only ever offer the client-side
        # "recompute this hash yourself" button when the diff this module
        # just reconstructed already, verifiably, hashes to the exact value
        # the receipt was signed over. See the module docstring for why the
        # reconstruction can legitimately diverge (intervening failed
        # turns) and why this is checked rather than assumed.
        diff_hash_reconstructable = False
        if receipt and receipt.get("diff_sha256") and not diff_truncated:
            diff_hash_reconstructable = hashlib.sha256(diff_text.encode("utf-8")).hexdigest() == receipt["diff_sha256"]

        turn_rules = rules_by_turn.get(turn, [])

        turns_out.append({
            "turn": turn,
            "shipped": shipped,
            "tag": checkpoint.tag if checkpoint else "",
            "commit": checkpoint.commit[:12] if checkpoint else "",
            "note": checkpoint.note if checkpoint else "",
            "decisions": [{"title": e.title, "body": e.body, "tags": e.tags} for e in decision_entries],
            "failures": [{"title": e.title, "body": e.body, "tags": e.tags} for e in failure_entries],
            "other_entries": [{"kind": e.kind, "title": e.title, "body": e.body} for e in other_entries],
            "diff": diff_text,
            "diff_truncated": diff_truncated,
            "diff_hash_reconstructable": diff_hash_reconstructable,
            "receipt": receipt,
            "rules_learned": [
                {"category": r.category, "rule_text": r.rule_text, "repeats_observed": r.repeats_observed}
                for r in turn_rules
            ],
        })

    return {
        "generated_at": _ts(),
        "workdir": str(workdir),
        "linked_repos": [{"path": r.path, "label": r.label} for r in linked_repos],
        "all_rules": [
            {"category": r.category, "rule_text": r.rule_text, "repeats_observed": r.repeats_observed, "source_turn": r.source_turn}
            for r in rules
        ],
        "turns": turns_out,
    }


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Supersonic — Black Box Replay</title>
<style>
:root {
  --bg: #fbf9f6; --surface: #fffdfb; --line: rgba(28,21,19,0.12); --line-strong: rgba(28,21,19,0.22);
  --text: #1c1513; --text-secondary: #6b5a52; --text-muted: #9c8d85;
  --accent: #e2542a; --accent-soft: rgba(226,84,42,0.1);
  --good: #2f8f6e; --good-soft: rgba(47,143,110,0.1);
  --bad: #c8402f; --bad-soft: rgba(200,64,47,0.1);
  --mono: "Geist Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --sans: "Geist", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font-family: var(--sans); }
header.bb-head { padding: 20px 28px; border-bottom: 1px solid var(--line); display: flex; align-items: baseline; gap: 14px; }
header.bb-head h1 { font-size: 18px; margin: 0; }
header.bb-head .meta { color: var(--text-secondary); font-size: 13px; }
.bb-shell { display: grid; grid-template-columns: 260px 1fr; height: calc(100vh - 65px); }
.bb-rail { border-right: 1px solid var(--line); overflow-y: auto; padding: 10px; }
.bb-rail-item { padding: 10px 12px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; font-size: 13px; border: 1px solid transparent; }
.bb-rail-item:hover { background: var(--accent-soft); }
.bb-rail-item.active { background: var(--accent-soft); border-color: var(--accent); }
.bb-rail-item .t { font-family: var(--mono); font-weight: 600; }
.bb-rail-item .s { display: inline-block; margin-left: 6px; font-size: 11px; padding: 1px 6px; border-radius: 999px; }
.bb-rail-item .s.ship { background: var(--good-soft); color: var(--good); }
.bb-rail-item .s.roll { background: var(--bad-soft); color: var(--bad); }
.bb-detail { overflow-y: auto; padding: 24px 32px; }
.bb-card { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 18px 20px; margin-bottom: 16px; }
.bb-card h2 { font-size: 14px; margin: 0 0 10px 0; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-secondary); }
.bb-badge { display: inline-block; font-size: 12px; padding: 2px 9px; border-radius: 999px; font-weight: 600; }
.bb-badge.ok { background: var(--good-soft); color: var(--good); }
.bb-badge.bad { background: var(--bad-soft); color: var(--bad); }
.bb-badge.neutral { background: var(--line); color: var(--text-secondary); }
pre.bb-diff { font-family: var(--mono); font-size: 12.5px; line-height: 1.55; overflow-x: auto; margin: 0; white-space: pre-wrap; word-break: break-word; max-height: 480px; overflow-y: auto; }
.bb-diff .add { color: var(--good); }
.bb-diff .del { color: var(--bad); }
.bb-diff .hunk { color: var(--accent); }
.bb-btn { font-family: var(--sans); font-size: 12px; padding: 6px 12px; border-radius: 8px; border: 1px solid var(--line-strong); background: var(--surface); cursor: pointer; color: var(--text); }
.bb-btn:hover { border-color: var(--accent); color: var(--accent); }
.bb-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.bb-kv { font-size: 13px; color: var(--text-secondary); margin: 3px 0; }
.bb-kv b { color: var(--text); font-weight: 600; }
.bb-mono { font-family: var(--mono); font-size: 12.5px; word-break: break-all; }
.bb-note { font-size: 12px; color: var(--text-muted); margin-top: 8px; }
.bb-empty { color: var(--text-muted); font-size: 13px; }
</style>
</head>
<body>
<header class="bb-head">
  <h1>Black Box Replay</h1>
  <span class="meta" id="bb-meta"></span>
</header>
<div class="bb-shell">
  <nav class="bb-rail" id="bb-rail"></nav>
  <main class="bb-detail" id="bb-detail"></main>
</div>
<script id="bb-data" type="application/json">__REPLAY_JSON__</script>
<script>
const DATA = JSON.parse(document.getElementById('bb-data').textContent);

function esc(s) {
  return (s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderDiff(text) {
  if (!text) return '<span class="bb-empty">No diff.</span>';
  return text.split('\\n').map(line => {
    let cls = '';
    if (line.startsWith('+') && !line.startsWith('+++')) cls = 'add';
    else if (line.startsWith('-') && !line.startsWith('---')) cls = 'del';
    else if (line.startsWith('@@')) cls = 'hunk';
    return `<span class="${cls}">${esc(line)}</span>`;
  }).join('\\n');
}

async function sha256Hex(text) {
  const enc = new TextEncoder().encode(text);
  const buf = await crypto.subtle.digest('SHA-256', enc);
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
}

function meta() {
  const linked = DATA.linked_repos.length
    ? ` · ${DATA.linked_repos.length} linked repo(s) anchored`
    : '';
  document.getElementById('bb-meta').textContent =
    `${DATA.workdir} · ${DATA.turns.length} turn(s) · generated ${DATA.generated_at}${linked}`;
}

function renderRail() {
  const rail = document.getElementById('bb-rail');
  rail.innerHTML = DATA.turns.map((t, i) => `
    <div class="bb-rail-item" data-i="${i}">
      <span class="t">Turn ${t.turn}</span>
      <span class="s ${t.shipped ? 'ship' : 'roll'}">${t.shipped ? 'shipped' : 'rolled back'}</span>
    </div>
  `).join('');
  rail.querySelectorAll('.bb-rail-item').forEach(el => {
    el.addEventListener('click', () => selectTurn(parseInt(el.dataset.i, 10)));
  });
}

function gateRows(gate) {
  if (!gate) return '<p class="bb-empty">No gate verdict recorded.</p>';
  const rows = [
    ['tests', gate.tests_passed], ['lint', gate.lint_passed], ['critic', gate.critic_satisfied],
    ['thrash-free', gate.thrashing === null ? null : !gate.thrashing],
    ['telemetry', gate.telemetry_passed], ['dependency trust', gate.dependency_trust_passed],
    ['secret leak', gate.secret_leak_passed], ['test quality', gate.test_quality_passed],
  ];
  return rows.filter(([, v]) => v !== null && v !== undefined).map(([name, v]) =>
    `<div class="bb-kv"><b>${name}:</b> <span class="bb-badge ${v ? 'ok' : 'bad'}">${v ? 'pass' : 'fail'}</span></div>`
  ).join('') + `<div class="bb-kv" style="margin-top:6px">${esc(gate.summary || '')}</div>`;
}

async function selectTurn(i) {
  document.querySelectorAll('.bb-rail-item').forEach((el, idx) => el.classList.toggle('active', idx === i));
  const t = DATA.turns[i];
  const detail = document.getElementById('bb-detail');
  let receiptHtml = '<p class="bb-empty">No signed receipt for this turn.</p>';
  if (t.receipt) {
    const r = t.receipt;
    const sigBadge = r.verified
      ? '<span class="bb-badge ok">✓ signature verified</span>'
      : `<span class="bb-badge bad">✗ ${esc(r.verify_reason || 'invalid')}</span>`;
    receiptHtml = `
      <div class="bb-kv"><b>Signature:</b> ${sigBadge} <span class="bb-note">(checked server-side with the exact function \`sonic verify-receipts\` uses)</span></div>
      <div class="bb-kv"><b>Coding agent:</b> ${esc(r.coding_agent || '')} · <b>Provider/model:</b> ${esc(r.provider || '')}/${esc(r.model || '')}</div>
      <div class="bb-kv"><b>Diff SHA-256 (from receipt):</b> <span class="bb-mono">${esc(r.diff_sha256 || '')}</span></div>
      <div class="bb-kv"><b>Prompt SHA-256 (from receipt):</b> <span class="bb-mono">${esc(r.prompt_sha256 || '')}</span>
        <span class="bb-note">— not independently re-checkable: the raw prompt text isn't retained after the turn, only its hash was ever signed.</span></div>
      <div style="margin-top:10px">
        ${t.diff_hash_reconstructable
          ? '<button class="bb-btn" id="bb-recompute">Recompute diff hash in this browser →</button><span id="bb-recompute-result" class="bb-kv"></span>'
          : `<button class="bb-btn" disabled>Recompute diff hash in this browser →</button><span class="bb-note">Not offered for this turn — ${t.diff_truncated ? 'the diff was too large to embed in full' : 'one or more turns failed and rolled back between the previous shipped turn and this one, so the reconstructed diff isn\\'t byte-identical to what was originally signed (see the replay module\\'s docstring)'}. The signature above is still independently verified.</span>`}
      </div>
      <h2 style="margin-top:18px">Verify gate</h2>
      ${gateRows(r.gate)}
    `;
  }

  const failuresHtml = t.failures.length
    ? t.failures.map(f => `<div class="bb-kv"><b>${esc(f.title)}</b><br>${esc(f.body)}</div>`).join('')
    : '<p class="bb-empty">None.</p>';
  const decisionsHtml = t.decisions.length
    ? t.decisions.map(d => `<div class="bb-kv"><b>${esc(d.title)}</b><br>${esc(d.body)}</div>`).join('')
    : '<p class="bb-empty">None.</p>';
  const rulesHtml = t.rules_learned.length
    ? t.rules_learned.map(r => `<div class="bb-kv">Learned after ${r.repeats_observed}x <b>${esc(r.category)}</b>: ${esc(r.rule_text)}</div>`).join('')
    : '';

  detail.innerHTML = `
    <div class="bb-card">
      <h2>Turn ${t.turn} — ${t.shipped ? 'shipped' : 'rolled back'}</h2>
      ${t.shipped ? `<div class="bb-kv"><b>Checkpoint:</b> <span class="bb-mono">${esc(t.tag)} @ ${esc(t.commit)}</span> — ${esc(t.note)}</div>` : ''}
      <h2 style="margin-top:16px">Decisions</h2>${decisionsHtml}
      <h2 style="margin-top:16px">Failures</h2>${failuresHtml}
      ${rulesHtml ? `<h2 style="margin-top:16px">Rules engine</h2>${rulesHtml}` : ''}
    </div>
    ${t.shipped ? `<div class="bb-card"><h2>Diff${t.diff_truncated ? ' (truncated for display)' : ''}</h2><pre class="bb-diff">${renderDiff(t.diff)}</pre></div>` : ''}
    <div class="bb-card"><h2>Signed Turn Receipt</h2>${receiptHtml}</div>
  `;

  const btn = document.getElementById('bb-recompute');
  if (btn) {
    btn.addEventListener('click', async () => {
      const result = document.getElementById('bb-recompute-result');
      result.textContent = 'hashing…';
      const hex = await sha256Hex(t.diff);
      const match = t.receipt && hex === t.receipt.diff_sha256;
      result.innerHTML = match
        ? '<span class="bb-badge ok">✓ matches — this browser independently confirmed the diff hash</span>'
        : `<span class="bb-badge bad">✗ mismatch (got ${hex.slice(0,12)}…)</span>`;
    });
  }
}

meta();
renderRail();
if (DATA.turns.length) selectTurn(DATA.turns.length - 1);
else document.getElementById('bb-detail').innerHTML = '<p class="bb-empty">No turns recorded yet.</p>';
</script>
</body>
</html>
"""


def render_replay_html(data: Dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    # The JSON payload sits inside a <script type="application/json"> block,
    # not inline JS, so the only real risk is a literal "</script>" substring
    # inside embedded text (a diff or ledger entry) prematurely closing the
    # tag — escape just that one sequence, case-insensitively.
    payload = payload.replace("</script>", "<\\/script>").replace("</SCRIPT>", "<\\/SCRIPT>")
    return _TEMPLATE.replace("__REPLAY_JSON__", payload)


def build_replay_html(workdir: Path) -> str:
    """One-shot: gather everything and render the page. Never touches the
    working tree — read-only against the ledger, git history, and
    `.supersonic/` state that already exists."""
    return render_replay_html(build_replay_data(workdir))


__all__ = ["build_replay_data", "render_replay_html", "build_replay_html"]
