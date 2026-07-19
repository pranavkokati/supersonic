# Supersonic

**Ships fast. Tells you exactly what to check before you trust it.**

[![License: MIT](https://img.shields.io/badge/License-MIT-pink.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)

Supersonic is a local, open-source autonomous build loop. It plans, checkpoints, builds, verifies, and ships —
and unlike a single-call router that blindly decides what happens next, it can only keep a turn's changes once
they clear an independent, up-to-eight-signal Verify gate. A failed turn is rolled back to the last proven-good
state, not left to compound into a worse one.

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

- **Continuity Graph, not a truncated transcript.** Instead of compressing a flat context blob down to fit a
  token budget, Supersonic keeps an append-only, git-committed ledger of structured facts — decisions,
  invariants, failures, and distilled lessons. Each turn retrieves only what's relevant to its goal, with
  invariants and open failures always included regardless of relevance score, because silently dropping a
  constraint is far more expensive than a few extra tokens of context.

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

## Pipeline

| Stage | What happens |
|---|---|
| Plan | One provider-agnostic call grounds the idea and writes a build plan + product name |
| Checkpoint | Git-native commit + tag of the current verified state |
| Build | Claude Code, Codex, OpenCode, Cursor Agent, or Aider — bring your own coding-agent CLI |
| Dependency Trust | Newly-added packages checked against the real PyPI/npm registry — nonexistent fails the turn outright |
| Secret Leak | Added diff lines scanned for the structural shape of a real credential — a match fails the turn outright |
| Test Quality | Once real tests pass, bounded AST mutants scoped to touched functions are re-tested against the suite — a fair-vote signal |
| Verify | Tests + lint/typecheck + goal-satisfaction critic + thrash detector + the above, combined into one pass/fail gate |
| Signed Receipt | An Ed25519-signed prompt/diff/gate attestation is written into the same commit as the checkpoint below |
| Rollback or ship | Pass → new checkpoint, pushed to GitHub. Fail → hard reset, failure logged to the Continuity Graph |
| Review Risk | Every file in a shipped turn ranked by blast radius, sensitive-path exposure, and missing test coverage |

The loop stops when the planner marks the build genuinely complete *and* the latest verification passed. The
configured `max_turn_budget` remains a hard ceiling regardless.

## Project layout

```text
supersonic/          Python package
  providers/          LLM provider abstraction (Anthropic, OpenAI, Ollama) — auto-detected
  memory/             Continuity Graph — ledger, retrieval, distillation
  loop/               Checkpoint / Rollback / Planner / Orchestrator
  verify/             Tests, lint, critic, thrash, dependency trust, secret leak, test quality, receipts, combined gate
  agents/             Coding-agent CLI runner
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
