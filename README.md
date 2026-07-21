# Supersonic

**Ships fast. Tells you exactly what to check before you trust it.**

[![License: MIT](https://img.shields.io/badge/License-MIT-pink.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)

Supersonic is a local, open-source autonomous build loop. It plans, checkpoints, builds, verifies, and ships —
and unlike a single-call router that blindly decides what happens next, it can only keep a turn's changes once
they clear an independent, up-to-eight-signal Verify gate. A failed turn is rolled back to the last proven-good
state, not left to compound into a worse one.

## Black Box Replay

Every build produces `sonic replay <workdir>` — a single, self-contained HTML file, no server or network
required, that opens in any browser as a scrubbable timeline of the entire build: every turn's diff, its full
Verify gate breakdown, its Signed Turn Receipt, and any rule the Self-Evolving Rules Engine learned. It's built
entirely from data Supersonic already writes to disk — the Continuity Graph ledger, the git checkpoint history,
Signed Turn Receipts, `.supersonic/rules.md` — so it costs nothing extra to generate and there's nothing to
fabricate: every claim on the page is either read straight off disk or independently re-derived in your
browser.

Two specific things it verifies, and one it's explicit about *not* verifying:

- **Signature verification is server-side**, using the exact same `verify_receipt_file()` function
  `sonic verify-receipts` already uses — this page does not reimplement Ed25519 in JavaScript. A from-scratch
  client-side verifier would have to reproduce Python's exact canonical-JSON byte encoding to check a signature
  correctly, and getting that byte-for-byte right for every payload shape is a real, easy-to-get-subtly-wrong
  problem. A security feature that's subtly wrong is worse than one that's honestly server-checked once.
- **Diff hashes are re-derived client-side**, in your browser, with no trust placed in whatever generated the
  page: the Web Crypto API's SHA-256 over the exact diff text embedded on the page, compared live against that
  turn's receipt. Before it offers this button at all, the page generator checks its own work — it hashes the
  diff it just reconstructed and compares it to the receipt's stored hash, server-side, and only exposes the
  button where that check actually passes. A turn with an intervening failed-and-rolled-back turn before it (or
  a diff too large to embed in full) shows the diff for reading, with an honest note about why independent
  recomputation isn't offered for it — not a silently wrong "verified" badge.
- **Prompt hashes are not independently re-checkable, and the page says so.** The raw prompt text isn't
  retained past the turn that used it — only its SHA-256 fingerprint is, by design, since keeping every
  historical prompt verbatim would bloat every checkout indefinitely. The fingerprint is still covered by the
  receipt's Ed25519 signature; it just can't be recomputed from scratch here.

See `supersonic/loop/replay.py` for the full implementation and its extended honesty notes.

## What makes this different

Almost every agentic coding tool competes on the same axis right now: how fast and how autonomously it can
produce a change that passes its own checks. That axis is close to saturated — checkpoint/verify/rollback
loops are table stakes across the current generation of tools. What's still unsolved: a controlled study of
experienced developers using AI coding tools found task time went *up*, not down, because verification
overhead outweighed the generation speedup. Separately, the hardest class of AI-introduced bug isn't the one
that fails a test — it's the one that compiles, passes lint, and satisfies a goal-satisfaction critic while
quietly touching an auth check or a payment path with no corresponding test change. Supersonic is built
around three specific answers to those problems:

- **Review Risk.** Every turn that ships gets its changed files ranked by blast radius (how many other files
  import it), sensitive-path exposure (auth/payment/permissions/migration keyword matches against the diff),
  and test-coverage delta (did a matching test file change in the same turn). A static heuristic, not an LLM
  call — instant, and it tells you *where* to spend your five minutes of review time instead of handing you
  an undifferentiated diff. See `supersonic/verify/review_risk.py` for the exact scoring.

- **Checkpoint → Verify → Rollback.** Every turn is git-checkpointed before it runs. Verification checks up to
  eight independent signals — tests, lint/typecheck, an LLM goal-satisfaction critic, a diff-similarity thrash
  detector, an optional browser telemetry check, and three pre-flight gates described below — and a turn is
  only kept if enough of the signals that actually ran come back positive. A failed turn is hard-reset to the
  last verified checkpoint, and the failure reason is written to permanent memory so the next attempt doesn't
  repeat it. Review Risk and Signed Turn Receipts only ever look at a turn that already passed this.

- **Dependency Trust Gate.** Every newly-added package in a turn's diff is checked against the real PyPI/npm
  registry before it ships. A nonexistent package fails the turn outright — the exact pattern behind
  "slopsquatting," where an attacker pre-registers the name an agent is statistically likely to hallucinate.
  A hard veto, not a vote: see `supersonic/verify/dependency_trust.py`.

- **Secret Leak Gate.** Every turn's added diff lines are scanned for the structural shape of a real
  credential — AWS keys, PEM blocks, GitHub/Slack/Stripe/Anthropic/OpenAI/Google tokens, a new committed
  `.env` file. A high-confidence match fails the turn outright, same severity as a syntax error. Also a hard
  veto: see `supersonic/verify/secret_leak.py`.

- **Test Quality Gate.** Once a turn's real tests pass, a small bounded set of AST-level mutants (comparison/
  boolean/constant flips), scoped only to the functions the turn touched, are re-tested against the same
  suite. A mutant the suite doesn't catch means a test that passes without actually verifying that logic — the
  one failure mode "tests: PASS" can't see by definition. Unlike the two gates above, this is a fair-vote
  signal, not a veto: see `supersonic/verify/test_quality.py`.

- **Signed Turn Receipts.** Every turn that ships gets an Ed25519-signed JSON attestation — prompt hash, diff
  hash, full Verify gate verdict, provider/model, coding agent — written to
  `.supersonic/receipts/turn-<n>.json` in the *same commit* as the checkpoint it describes. Verify any receipt
  offline, from any checkout, with `sonic verify-receipts <path>` — the public key travels inside the receipt
  itself. Not a Verify signal; it never blocks a turn, it's a reproducibility record for one that already
  shipped. See `supersonic/verify/receipts.py`.

- **Risk-Aware Model Escalation.** When a shipped turn's Review Risk brief flags a HIGH-risk file (large blast
  radius, a sensitive-path match, or no matching test change), the *next* turn — and only the next turn —
  automatically runs at a stronger model, both for the coding-agent CLI itself (a verified `--model`/`-m` flag
  for Claude Code, Codex, OpenCode, Cursor Agent, or Aider) and for Supersonic's own critic call. It decays
  back to the default model the turn after, so routine turns stay fast and cheap and only the turns that just
  touched risky territory pay for the stronger model. Off by default with no risky-file history, and it never
  overrides a model you haven't explicitly configured for escalation — see `supersonic/loop/orchestrator.py`
  and the "Risk-aware model escalation" settings card in the dashboard.

- **Continuity Graph, not a truncated transcript.** Instead of compressing a flat context blob down to fit a
  token budget, Supersonic keeps an append-only, git-committed ledger of structured facts — decisions,
  invariants, failures, and distilled lessons. Each turn retrieves only what's relevant to its goal, with
  invariants and open failures always included regardless of relevance score, because silently dropping a
  constraint is far more expensive than a few extra tokens of context.

- **PTY-native execution.** An optional (off by default, POSIX-only) execution mode that runs the coding-agent
  CLI inside a real pseudo-terminal (`pty.fork()`) instead of a plain subprocess pipe, so a CLI that
  special-cases "no terminal attached" (disabling colors, progress bars, interactive confirmations) behaves
  the way it would for a human typing the command directly. To be precise about what this is and isn't: a PTY
  governs a process's stdin/stdout, never its filesystem writes — this is not a claim of intercepting a file
  write before it hits disk (that would require ptrace/seccomp syscall interception or a FUSE overlay
  filesystem, neither of which this implements). Falls back to the standard subprocess path automatically on
  any platform or CLI it can't run cleanly on. See `supersonic/agents/pty_runner.py`.

- **Live Syntax Watch.** A concurrent background thread — mtime polling plus `ast.parse`, no kernel hooks —
  that re-checks every touched Python file within a fraction of a second of it being saved, *while the agent
  is still writing files*, not just at the end of the turn like Syntax Shield's diff-based check (which still
  runs as the authoritative gate afterward). Observability only in this version: it surfaces the exact broken
  file and line early, it does not pause or interrupt the agent process. See
  `supersonic/verify/live_syntax_watch.py`.

- **Self-Evolving Rules Engine.** When the exact same Verify failure category repeats across turns at least
  `rules_evolution_min_repeats` times, one supervisor-critic LLM call synthesizes a single concise, durable
  rule from the specific failure trace and appends it — never rewrites or randomly mutates an existing one —
  to the project's own `.supersonic/rules.md`, folded into every subsequent turn's prompt. Best-effort, it
  also mirrors into `.cursorrules`/`CLAUDE.md` if the project already has one of those real convention files,
  updating only its own clearly marked section and leaving everything else untouched — never creating one of
  those files from scratch. Narrower by design than genetic-algorithm prompt mutation (OpenEvolve/EvoAgentX's
  actual approach): one durable rule per repeated failure category, not a population of candidate prompts
  scored against a fitness function. See `supersonic/memory/rules_engine.py`.

- **Multi-Repository State Anchoring.** Register the other git working directories a feature ticket actually
  spans — a frontend, a backend, a schema-definitions repo — and every turn snapshots all of them alongside
  the primary repo. When the primary repo's turn ships, every linked repo's checkpoint refreshes to match;
  when it fails Verify and rolls back, every linked repo rolls back with it. The coding agent still runs
  exactly once per turn, against the primary workdir only — what's coordinated is checkpoint and rollback, so
  nothing in the linked set can silently drift out of sync with what the primary repo's Verify gate actually
  approved. See `supersonic/loop/multi_repo.py`.

No multi-agent racing, no bandit tuning, no doubled API spend chasing a marginal pick between two coding
agents — the Verify gate already rejects bad output regardless of which agent produced it. Everything else —
GitHub shipping, Linear, research enrichment, completion notifications — is optional and off unless you
configure it. The only hard requirement to run Supersonic is **one LLM API key**: Anthropic, OpenAI, or a
local Ollama server.

## Quickstart

```bash
git clone https://github.com/your-org/supersonic.git
cd supersonic
./bootstrap.sh
source .venv/bin/activate
sonic serve
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787) and complete onboarding — it asks for one API key.

## Run

```bash
sonic serve                                                     # local dashboard
sonic run --idea "Build a focused developer tool" --agent claude
sonic run --demo                                                 # no live provider/agent calls
```

## Verify setup

```bash
sonic doctor
```

Checks which LLM provider is configured, which coding-agent CLIs are on `PATH`, and whether `git`/`gh` are
available for shipping.

```bash
sonic verify-receipts <path-to-project>
```

Cryptographically verifies every Signed Turn Receipt in a project, offline — no access to the machine that
generated them required.

```bash
sonic replay <path-to-project> [--out replay.html]
```

Generates Black Box Replay — a single self-contained HTML timeline of the whole build. Open it in any browser,
offline, from any checkout. See "Black Box Replay" above.

## Pipeline

| Stage | What happens |
|---|---|
| Plan | One provider-agnostic call grounds the idea and writes a build plan + product name |
| Checkpoint | Git-native commit + tag of the current verified state (and every linked repo, if Multi-Repository State Anchoring is configured) |
| Build | Claude Code, Codex, OpenCode, Cursor Agent, or Aider — bring your own coding-agent CLI, optionally run inside a real PTY |
| Live Watch | Concurrent filesystem watcher flags a broken Python file within a fraction of a second of it being saved, mid-turn |
| Dependency Trust | Newly-added packages checked against the real PyPI/npm registry — nonexistent fails the turn outright |
| Secret Leak | Added diff lines scanned for the structural shape of a real credential — a match fails the turn outright |
| Test Quality | Once real tests pass, bounded AST mutants scoped to touched functions are re-tested against the suite — a fair-vote signal |
| Verify | Tests + lint/typecheck + goal-satisfaction critic + thrash detector + the above, combined into one pass/fail gate |
| Signed Receipt | An Ed25519-signed prompt/diff/gate attestation is written into the same commit as the checkpoint below |
| Rollback or ship | Pass → new checkpoint (+ every linked repo), pushed to GitHub. Fail → hard reset (+ every linked repo), failure logged to the Continuity Graph |
| Review Risk | Every file in a shipped turn ranked by blast radius, sensitive-path exposure, and missing test coverage |

The loop stops when the planner marks the build genuinely complete *and* the latest verification passed. The
configured `max_turn_budget` remains a hard ceiling regardless.

**Risk-aware model escalation** is a feedback loop across this table, not a stage in it: if a shipped turn's
Review Risk output has at least one HIGH-risk file, the *Build* and *Verify* stages of the very next turn run
at the stronger model configured for that coding agent (and for the critic), then fall back to the default the
turn after.

**The Self-Evolving Rules Engine** is likewise a feedback loop, not a stage: once a Verify failure category
repeats enough times, one supervisor-critic call writes a durable rule into the project's own rules file,
folded into every prompt from then on.

## Project layout

```text
supersonic/          Python package
  providers/          LLM provider abstraction (Anthropic, OpenAI, Ollama) — auto-detected
  memory/             Continuity Graph — ledger, retrieval, distillation; rules_engine.py — Self-Evolving Rules Engine
  loop/               Checkpoint / Rollback / Planner / Orchestrator; multi_repo.py — Multi-Repository State
                      Anchoring; replay.py — Black Box Replay
  verify/             Tests, lint, critic, thrash, dependency trust, secret leak, test quality, receipts, live syntax watch, combined gate
  agents/             Coding-agent CLI runner; pty_runner.py — optional PTY-native execution
  integrations/       Native git + gh CLI shipping, optional Linear, optional webhook notify
  research/           Optional Tavily enrichment — never required
app/                  Local onboarding + dashboard (FastAPI + vanilla JS, SSE live view)
web/                  Marketing site
tests/                Pytest suite
docs/                 Architecture, demo walkthrough, FAQ
```

- [Architecture](docs/ARCHITECTURE.md)
- [Demo walkthrough](docs/DEMO.md)
- [FAQ](docs/FAQ.md)
- [Roadmap](ROADMAP.md)

## Development

```bash
source .venv/bin/activate
python -m pytest tests/ -q
```

## License

MIT — see [LICENSE](LICENSE).
