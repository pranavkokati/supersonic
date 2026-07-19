"""Supersonic orchestrator — Checkpoint -> Verify -> Rollback build loop.

The whole system in one sentence: every turn is committed only if it earns
it. A turn runs, gets scored by the Verify gate on up to four independent
signals, and either becomes the new safe checkpoint or gets reverted — with
the failure reason written into the Continuity Graph so the next attempt
doesn't repeat it.

This replaces the previous generation's single-call blind router (one LLM
call decides run_agent/run_qa/done with a flat turn cap as the only safety
net) with a loop that can only move forward on verified evidence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from supersonic.agents.patch_mode import run_patch_diff_turn
from supersonic.agents.runner import AgentResult, CodingAgentRunner
from supersonic.config import UserSecrets, get_settings
from supersonic.events import publish
from supersonic.integrations import git_ops
from supersonic.integrations.github import ensure_repo, ship as github_ship
from supersonic.integrations.linear import create_issue as linear_create_issue, is_configured as linear_configured
from supersonic.integrations.notify import notify_completion
from supersonic.loop.checkpoint import Checkpoint, CheckpointManager
from supersonic.loop.dependency_mapper import build_target_graph
from supersonic.loop.planner import ProductBrand, TurnPlan, generate_brand, generate_plan, generate_turn_plan
from supersonic.loop.rollback import rollback_to
from supersonic.memory import ContinuityGraph, ContinuityLedger, distill, should_distill
from supersonic.providers import get_provider
from supersonic.providers.base import LLMProvider, ProviderError
from supersonic.research.tavily import TavilyResearch, is_configured as tavily_configured
from supersonic.research.web import model_knowledge_bundle
from supersonic.store import Run, append_agent_log, get_project, update_project, update_run
from supersonic.templates import apply_template
from supersonic.validate import validate_live_run
from supersonic.verify.critic import CriticVerdict
from supersonic.verify.dependency_trust import DependencyTrustVerdict, run_dependency_trust
from supersonic.verify.secret_leak import SecretLeakVerdict, run_secret_leak_gate
from supersonic.verify.gate import GateResult, build_qa_reprompt, run_gate
from supersonic.verify.qa import CheckResult, run_tests
from supersonic.verify.receipts import build_receipt, write_receipt
from supersonic.verify.review_risk import build_review_brief
from supersonic.verify.syntax_shield import run_syntax_shield
from supersonic.verify.telemetry_gate import TelemetryVerdict, run_telemetry_gate
from supersonic.verify.test_quality import TestQualityVerdict, run_test_quality_gate
from supersonic.verify.thrash import ThrashVerdict
from supersonic.webhooks import fire_webhook
from supersonic.workdir import workdir_summary

logger = logging.getLogger(__name__)

SAFETY_MAX_TURNS = 200


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunContext:
    """Owns event emission + phase bookkeeping for one run, mirrored to the store for SSE replay."""

    def __init__(self, run: Run, phases: List[Dict[str, Any]]):
        self.run = run
        self.phases = phases
        self.agent_log = ""

    def emit(self, event: Dict[str, Any]) -> None:
        event.setdefault("ts", _ts())
        publish(self.run.id, event)

    def start_phase(self, phase_id: str, tool: str, detail: str, *, stage: str = "loop") -> None:
        entry = {"phase": phase_id, "tool": tool, "detail": detail, "status": "running", "ts": _ts(), "stage": stage}
        self.phases.append(entry)
        update_run(self.run.id, status="running", current_phase=phase_id, phases=self.phases)
        self.emit({"type": "phase", "phase": phase_id, "tool": tool, "status": "running", "detail": detail, "stage": stage})

    def finish_phase(self, phase_id: str, detail: str, **extra: Any) -> None:
        for p in reversed(self.phases):
            if p["phase"] == phase_id and p["status"] == "running":
                p["status"] = "done"
                p["detail"] = detail
                p.update(extra)
                break
        update_run(self.run.id, phases=self.phases)
        stage = extra.get("stage", "loop")
        self.emit({"type": "phase", "phase": phase_id, "status": "done", "detail": detail, "stage": stage,
                    **{k: v for k, v in extra.items() if k != "stage"}})

    def agent_line(self, line: str) -> None:
        self.agent_log += line + "\n"
        append_agent_log(self.run.id, line)
        self.emit({"type": "agent_line", "line": line})

    def checkpoint_event(self, checkpoint: Checkpoint, verified: bool) -> None:
        self.emit({"type": "checkpoint", "verified": verified, **checkpoint.to_dict()})

    def ledger_event(self, kind: str, title: str, turn: int) -> None:
        self.emit({"type": "ledger_entry", "kind": kind, "title": title, "turn": turn})

    def gate_event(self, turn: int, gate: GateResult) -> None:
        self.emit({"type": "verify_result", "turn": turn, **gate.to_dict()})

    def dle_stage_event(self, stage: str, status: str, detail: str = "") -> None:
        """Live progress for the DLE panel. `stage` is one of factor/patch/shield/
        telemetry; `status` is pending/running/pass/fail/skipped. "Ship" is not
        emitted here — the dashboard derives it from the existing checkpoint event."""
        self.emit({"type": "dle_stage", "stage": stage, "status": status, "detail": detail})

    def review_brief_event(self, brief) -> None:
        """Fired once per shipped turn — the ranked "what to actually read"
        list from verify/review_risk.py. SSE-only, same as dle_stage_event;
        not persisted to the store, the dashboard renders it live."""
        self.emit({"type": "review_brief", **brief.to_dict(), "summary": brief.summary_line()})

    def receipt_event(self, receipt) -> None:
        """Fired once per shipped turn — the signed reproducibility record
        from verify/receipts.py. SSE-only, same treatment as review_brief_event;
        the dashboard renders a short verified badge, it never blocks a turn."""
        self.emit({"type": "turn_receipt", **receipt.to_dict()})


def _pick_idea(secrets: UserSecrets, provider: Optional[LLMProvider], seed: str, project_idea: str, demo: bool) -> tuple:
    seed = (seed or project_idea or "").strip()
    if demo:
        return seed or "Local-first developer automation tool", []
    if tavily_configured(secrets):
        try:
            bundle = TavilyResearch(secrets).search_ideas(seed)
            idea = seed or (bundle.answer.split(".")[0][:200] if bundle.answer else "") or seed
            return idea or "Local-first developer automation tool", [bundle.to_context_block()]
        except Exception:
            logger.exception("Tavily research failed, continuing without it")
    if seed:
        return seed, []
    bundle = model_knowledge_bundle(provider, "developer tooling opportunities")
    return "Local-first developer automation tool", [bundle.to_context_block()] if bundle.answer else []


def _build_prompt(
    *, idea: str, plan: str, brand: ProductBrand, goal: str, turn: int, continuity_context: str,
    dependency_context: str = "",
) -> str:
    brand_block = brand.to_context_block()
    header = f"# Supersonic — build turn {turn}" if turn > 1 else "# Supersonic — build turn 1 (kickoff)"
    task_block = f"## Task\n{goal}" if turn > 1 else f"## Product idea\n{idea}"
    dependency_block = f"\n{dependency_context}\n" if dependency_context else ""
    return f"""{header}

{task_block}

{brand_block}

## Build plan
{plan}

{continuity_context}
{dependency_block}
## Instructions
- Push toward the plan concretely — real source files, not just markdown.
- Respect every invariant listed above; do not repeat any listed known failure.
- Include or update tests for anything you add.
- Commit is handled by the loop — just leave the working tree in the state you want evaluated.

Build now.
"""


def run_factory(run: Run, secrets: UserSecrets, seed: str = "") -> Dict[str, Any]:
    settings = get_settings()
    demo = settings.sonic_demo
    ctx = RunContext(run, list(run.phases))
    project = get_project(run.project_id)
    if not project:
        raise ValueError("project not found")

    agent_kind = project.agent  # type: ignore
    if not demo:
        validate_live_run(secrets, agent_kind)  # type: ignore

    workdir = Path(project.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    template_id = getattr(project, "template_id", "greenfield") or "greenfield"
    template_hint = apply_template(workdir, template_id, project.idea or seed)

    update_run(run.id, status="running", agent_log="")
    ctx.emit({"type": "started", "project_id": project.id, "agent": agent_kind, "orchestration": "checkpoint_verify_rollback"})

    provider: Optional[LLMProvider] = None
    if not demo:
        try:
            provider = get_provider(secrets)
        except ProviderError as e:
            update_run(run.id, status="failed", error=str(e), current_phase="", finished_at=_ts())
            ctx.emit({"type": "error", "message": str(e)})
            raise

    build_complete = False
    try:
        # ---- setup (once) ----
        ctx.start_phase("research", "Research", "Grounding the idea…", stage="setup")
        idea, research_blocks = _pick_idea(secrets, provider, seed, project.idea, demo)
        ctx.finish_phase("research", f"Idea: {idea[:120]}", stage="setup")
        if template_hint:
            (workdir / "TEMPLATE.md").write_text(template_hint, encoding="utf-8")
        update_project(project.id, idea=idea, name=idea[:80] or project.name, status="planning")

        ctx.start_phase("checkpoint-init", "Checkpoint", "Initializing git-native checkpointing…", stage="setup")
        checkpoints = CheckpointManager(workdir)
        ledger = ContinuityLedger(workdir)
        graph = ContinuityGraph(ledger)
        ctx.finish_phase("checkpoint-init", "Repo ready", stage="setup")

        ctx.start_phase("plan", "Planner", "Build plan…", stage="setup")
        plan = _fallback_plan(idea) if demo else generate_plan(provider, idea, research_blocks)  # type: ignore
        (workdir / "BUILD_PLAN.md").write_text(plan, encoding="utf-8")
        ctx.finish_phase("plan", "Plan ready", stage="setup")

        ctx.start_phase("brand", "Planner", "Naming…", stage="setup")
        brand = ProductBrand.from_idea(idea) if demo else generate_brand(provider, idea, plan)  # type: ignore
        ctx.finish_phase("brand", f"{brand.product_name} · {brand.repo_slug}", stage="setup")

        ledger.record_decision(0, "Adopted build plan", plan, tags=["setup"])
        ledger.record_invariant(0, "Keep the working tree buildable", "Every turn must leave the project in a runnable state.")
        ledger.render_brain()

        github_url = None
        if not demo:
            ctx.start_phase("ship-init", "GitHub", "Repo setup…", stage="setup")
            github_url = ensure_repo(workdir, brand.repo_slug, private=True, description=brand.tagline)
            ctx.finish_phase("ship-init", github_url or "Skipped (gh not available/authenticated)", stage="setup", github_url=github_url or "")

        linear_url = None
        if not demo and linear_configured(secrets):
            linear_url = linear_create_issue(secrets, f"[Supersonic] {brand.product_name}", plan)

        init_checkpoint = checkpoints.create(0, "setup complete")
        ctx.checkpoint_event(init_checkpoint, verified=True)
        last_good = init_checkpoint

        ctx.emit({"type": "setup_complete", "github_url": github_url or "", "linear_url": linear_url or ""})

        # ---- loop ----
        recent_diffs: List[str] = []
        next_follow_up = ""
        turns_completed = 0
        last_gate: Optional[GateResult] = None
        max_turns = min(SAFETY_MAX_TURNS, max(secrets.max_turn_budget, 1))
        chosen_agent = agent_kind
        # Rolling baseline for the DLE telemetry gate's perf-regression check —
        # each turn's post-change median becomes the next turn's pre-change
        # baseline. Empty until the first successful telemetry run, at which
        # point regression detection naturally has nothing to compare against
        # (see verify/telemetry_gate.compute_regression).
        telemetry_baseline: List[float] = []
        # Risk-Aware Model Escalation: True whenever the most recently shipped
        # turn's Review Risk brief flagged at least one HIGH-risk file. Only
        # ever set from inside the `if gate.passed:` branch after a review
        # brief is computed — left untouched by a rollback, so a failed turn
        # doesn't erase what an earlier shipped turn found.
        escalate_next_turn = False
        AGENT_ESCALATION_MODELS = {
            "claude": secrets.escalation_model_claude, "codex": secrets.escalation_model_codex,
            "opencode": secrets.escalation_model_opencode, "cursor": secrets.escalation_model_cursor,
            "aider": secrets.escalation_model_aider,
        }

        turn = 0
        while turn < max_turns:
            turn += 1
            turns_completed = turn
            goal = idea if turn == 1 else (next_follow_up or "Continue building toward the plan.")

            # Risk-Aware Model Escalation — decided once per turn, before the
            # agent runs, from the PRIOR turn's outcome only (never this
            # turn's, which hasn't happened yet). See dle_risk_escalation.
            escalation_active = not demo and secrets.dle_risk_escalation and escalate_next_turn
            agent_model_override: Optional[str] = None
            critic_model_override: Optional[str] = None
            escalation_reason = ""
            if escalation_active:
                agent_model_override = (AGENT_ESCALATION_MODELS.get(chosen_agent) or "").strip() or None
                if provider is not None:
                    critic_model_override = provider.default_model
                escalation_reason = "previous turn shipped a HIGH-risk file"
                if agent_model_override or critic_model_override:
                    ctx.agent_line(
                        f"[risk escalation] {escalation_reason} — "
                        f"agent model: {agent_model_override or 'unchanged (no target configured)'}, "
                        f"critic model: {critic_model_override or 'unchanged (no provider)'}"
                    )

            ctx.emit({
                "type": "turn_started", "turn": turn, "goal": goal,
                "escalated": bool(agent_model_override or critic_model_override), "escalation_reason": escalation_reason,
            })

            retrieval = graph.retrieve(goal, token_budget=secrets.ledger_context_budget, current_turn=turn)

            # DLE stage 1 — Dependency Mapper: cheap static-import scoping hint,
            # folded into the prompt so the agent factors changes toward files
            # that actually relate to this turn's goal. See dependency_mapper.py
            # for why this is deliberately NOT LSP-grade resolution.
            dependency_context = ""
            if not demo and secrets.dle_dependency_mapper:
                ctx.dle_stage_event("factor", "running")
                try:
                    target_graph = build_target_graph(workdir, goal)
                    dependency_context = target_graph.to_context_block()
                    ctx.dle_stage_event(
                        "factor", "pass",
                        f"{len(target_graph.files)} file(s) selected (keywords: {', '.join(target_graph.goal_keywords) or 'none'})",
                    )
                except Exception:
                    logger.exception("dependency mapper failed, continuing without it")
                    ctx.dle_stage_event("factor", "fail", "mapper error — continuing without a scoped file list")
            else:
                ctx.dle_stage_event("factor", "skipped", "demo mode" if demo else "disabled in settings")

            prompt = _build_prompt(
                idea=idea, plan=plan, brand=brand, goal=goal, turn=turn,
                continuity_context=retrieval.context_block, dependency_context=dependency_context,
            )

            ctx.start_phase(f"turn-{turn}", agent_kind.title(), f"Turn {turn}…", stage="loop")
            ctx.agent_line(f"─── Turn {turn} ───")

            gate: GateResult
            diff = ""

            if demo:
                agent_result = AgentResult(agent=agent_kind, success=True, output="[demo] simulated build turn", command="demo")
                ctx.agent_line(agent_result.output)
                diff = ""
                ctx.dle_stage_event("patch", "skipped", "demo mode")
                ctx.dle_stage_event("shield", "skipped", "demo mode")
                ctx.dle_stage_event("telemetry", "skipped", "demo mode")
                ctx.dle_stage_event("deptrust", "skipped", "demo mode")
                ctx.dle_stage_event("secretleak", "skipped", "demo mode")
                ctx.dle_stage_event("testquality", "skipped", "demo mode")
                gate = run_gate(workdir, provider=None, goal=goal, diff="", invariants=[], recent_diffs=[], min_signals_pass=secrets.verify_min_signals_pass)
            else:
                runner = CodingAgentRunner(chosen_agent, secrets)
                # Tracks the literal text of the LAST prompt actually sent to
                # the coding agent this turn — including whichever corrective
                # re-prompt, if any, is the one that produced the diff that
                # ends up shipping. Signed Turn Receipts hash this, not the
                # prompt *template*, so the receipt reflects what the agent
                # genuinely saw.
                effective_prompt = prompt

                # DLE stage 2 — patch-diff mode (optional). Falls back to the
                # normal full-file-rewrite path on any failure; never blocks a turn.
                if secrets.dle_patch_diff_mode:
                    ctx.dle_stage_event("patch", "running")
                    patch_result = run_patch_diff_turn(runner, prompt, workdir, on_line=ctx.agent_line, model=agent_model_override)
                    if patch_result.applied:
                        ctx.dle_stage_event("patch", "pass", f"applied cleanly in {patch_result.attempts} attempt(s)")
                        agent_result = patch_result.agent_result
                        ctx.agent_line(f"[patch-diff mode] applied cleanly in {patch_result.attempts} attempt(s)")
                    else:
                        ctx.dle_stage_event("patch", "fail", f"fell back to full-file rewrite: {patch_result.fallback_reason}")
                        ctx.agent_line(
                            f"[patch-diff mode] falling back to full-file rewrite: {patch_result.fallback_reason}"
                        )
                        agent_result = runner.run(prompt, workdir, on_line=ctx.agent_line, model=agent_model_override)
                else:
                    ctx.dle_stage_event("patch", "skipped", "disabled in settings")
                    agent_result = runner.run(prompt, workdir, on_line=ctx.agent_line, model=agent_model_override)

                diff = checkpoints.diff_since(last_good)

                # DLE stage 3 — Syntax Shield, before the expensive four-signal gate.
                shield_result = None
                if secrets.dle_syntax_shield:
                    ctx.dle_stage_event("shield", "running")
                    shield_result = run_syntax_shield(workdir, diff)
                    if shield_result.ran and not shield_result.ok:
                        ctx.agent_line("[syntax shield] syntax error detected — issuing one corrective re-prompt")
                        corrective_prompt = f"{prompt}\n\n{shield_result.reprompt}"
                        runner.run(corrective_prompt, workdir, on_line=ctx.agent_line, model=agent_model_override)
                        effective_prompt = corrective_prompt
                        diff = checkpoints.diff_since(last_good)
                        shield_result = run_syntax_shield(workdir, diff)
                    if shield_result.ran:
                        if shield_result.ok:
                            ctx.dle_stage_event("shield", "pass", f"{len(shield_result.checked_files)} file(s) checked, no syntax errors")
                        else:
                            ctx.dle_stage_event("shield", "fail", f"unresolved after re-prompt: {', '.join(shield_result.errors.keys())}")
                    else:
                        ctx.dle_stage_event("shield", "skipped", "no changed files to check")
                else:
                    ctx.dle_stage_event("shield", "skipped", "disabled in settings")

                if shield_result is not None and shield_result.ran and not shield_result.ok:
                    # Still broken after one auto-corrective re-prompt — skip the
                    # expensive gate entirely (tests/lint/critic would just fail
                    # for the same reason) and treat this as a failed turn.
                    broken = ", ".join(shield_result.errors.keys())
                    gate = GateResult(
                        passed=False,
                        signals_ran=1,
                        signals_passed=0,
                        tests=CheckResult(name="Tests"),
                        lint=CheckResult(name="Lint/typecheck"),
                        critic=CriticVerdict(),
                        thrash=ThrashVerdict(),
                        summary=f"Syntax Shield failed after one auto-corrective re-prompt: {broken}",
                    )
                else:
                    # DLE stage 4 — Telemetry Gate (optional fifth signal), auto-
                    # detected and auto-skipped when not applicable/available.
                    telemetry_verdict: Optional[TelemetryVerdict] = None
                    if secrets.dle_telemetry_gate:
                        ctx.dle_stage_event("telemetry", "running")
                        try:
                            telemetry_verdict = run_telemetry_gate(
                                workdir, enabled=True, baseline_samples=telemetry_baseline,
                            )
                            if telemetry_verdict.ran:
                                telemetry_baseline = [telemetry_verdict.current_median_ms] * 3
                                if telemetry_verdict.perf_regression:
                                    ctx.dle_stage_event(
                                        "telemetry", "fail",
                                        f"perf regression: {telemetry_verdict.baseline_median_ms:.0f}ms -> {telemetry_verdict.current_median_ms:.0f}ms",
                                    )
                                elif not telemetry_verdict.passed:
                                    ctx.dle_stage_event("telemetry", "fail", "console errors or layout issue detected")
                                else:
                                    ctx.dle_stage_event("telemetry", "pass", f"median {telemetry_verdict.current_median_ms:.0f}ms, no regression")
                            else:
                                ctx.dle_stage_event("telemetry", "skipped", telemetry_verdict.skipped_reason or "no frontend dev server detected")
                        except Exception:
                            logger.exception("telemetry gate failed unexpectedly, skipping this signal")
                            telemetry_verdict = None
                            ctx.dle_stage_event("telemetry", "skipped", "gate errored, skipped for this turn")
                    else:
                        ctx.dle_stage_event("telemetry", "skipped", "disabled in settings")

                    # DLE stage 5 — Dependency Trust Gate: check any newly-added
                    # package name in this turn's diff against the real PyPI/npm
                    # registry before paying for the expensive gate. A package
                    # that doesn't exist gets one corrective re-prompt, same
                    # pattern as Syntax Shield; still-nonexistent after that
                    # skips the rest of the gate entirely (it's failing either
                    # way, no reason to also spend a critic call on it).
                    dependency_trust_verdict: Optional[DependencyTrustVerdict] = None
                    if secrets.dle_dependency_trust:
                        ctx.dle_stage_event("deptrust", "running")
                        try:
                            dependency_trust_verdict = run_dependency_trust(workdir, diff)
                            if dependency_trust_verdict.ran and not dependency_trust_verdict.ok:
                                ctx.agent_line("[dependency trust] unverifiable package detected — issuing one corrective re-prompt")
                                corrective_prompt = f"{prompt}\n\n{dependency_trust_verdict.reprompt}"
                                runner.run(corrective_prompt, workdir, on_line=ctx.agent_line, model=agent_model_override)
                                effective_prompt = corrective_prompt
                                diff = checkpoints.diff_since(last_good)
                                dependency_trust_verdict = run_dependency_trust(workdir, diff)
                            if dependency_trust_verdict.ran:
                                if dependency_trust_verdict.ok:
                                    warn = f", {len(dependency_trust_verdict.suspicious)} flagged as new" if dependency_trust_verdict.suspicious else ""
                                    ctx.dle_stage_event("deptrust", "pass", f"{len(dependency_trust_verdict.trusted)} package(s) verified{warn}")
                                else:
                                    bad = ", ".join(f.name for f in dependency_trust_verdict.critical)
                                    ctx.dle_stage_event("deptrust", "fail", f"unresolved after re-prompt: {bad}")
                            else:
                                ctx.dle_stage_event("deptrust", "skipped", "no new dependencies in this turn")
                        except Exception:
                            logger.exception("dependency trust gate failed unexpectedly, skipping this signal")
                            dependency_trust_verdict = None
                            ctx.dle_stage_event("deptrust", "skipped", "gate errored, skipped for this turn")
                    else:
                        ctx.dle_stage_event("deptrust", "skipped", "disabled in settings")

                    if dependency_trust_verdict is not None and dependency_trust_verdict.ran and not dependency_trust_verdict.ok:
                        # Still unverifiable after one corrective re-prompt — skip
                        # the expensive gate (tests/lint/critic can't save a turn
                        # that's shipping a hallucinated dependency) and fail now.
                        bad = ", ".join(f.name for f in dependency_trust_verdict.critical)
                        gate = GateResult(
                            passed=False,
                            signals_ran=1,
                            signals_passed=0,
                            tests=CheckResult(name="Tests"),
                            lint=CheckResult(name="Lint/typecheck"),
                            critic=CriticVerdict(),
                            thrash=ThrashVerdict(),
                            summary=f"Dependency Trust Gate failed after one auto-corrective re-prompt: {bad}",
                            dependency_trust=dependency_trust_verdict,
                        )
                    else:
                        # DLE stage 6 — Secret Leak Gate: scan this turn's added
                        # diff lines for the structural shape of a real credential
                        # before paying for the expensive gate. Same one-corrective-
                        # reprompt-then-fail pattern as Dependency Trust and Syntax
                        # Shield — a leaked credential is a veto, not a vote.
                        secret_leak_verdict: Optional[SecretLeakVerdict] = None
                        if secrets.dle_secret_leak:
                            ctx.dle_stage_event("secretleak", "running")
                            try:
                                secret_leak_verdict = run_secret_leak_gate(workdir, diff)
                                if secret_leak_verdict.ran and not secret_leak_verdict.ok:
                                    ctx.agent_line("[secret leak] likely credential detected — issuing one corrective re-prompt")
                                    corrective_prompt = f"{prompt}\n\n{secret_leak_verdict.reprompt}"
                                    runner.run(corrective_prompt, workdir, on_line=ctx.agent_line, model=agent_model_override)
                                    effective_prompt = corrective_prompt
                                    diff = checkpoints.diff_since(last_good)
                                    secret_leak_verdict = run_secret_leak_gate(workdir, diff)
                                if secret_leak_verdict.ran:
                                    if secret_leak_verdict.ok:
                                        warn = f", {len(secret_leak_verdict.suspicious)} flagged as suspicious" if secret_leak_verdict.suspicious else ""
                                        ctx.dle_stage_event("secretleak", "pass", f"no unresolved credentials{warn}")
                                    else:
                                        bad = ", ".join(f.kind for f in secret_leak_verdict.critical)
                                        ctx.dle_stage_event("secretleak", "fail", f"unresolved after re-prompt: {bad}")
                                else:
                                    ctx.dle_stage_event("secretleak", "skipped", "no credential-shaped values in this turn")
                            except Exception:
                                logger.exception("secret leak gate failed unexpectedly, skipping this signal")
                                secret_leak_verdict = None
                                ctx.dle_stage_event("secretleak", "skipped", "gate errored, skipped for this turn")
                        else:
                            ctx.dle_stage_event("secretleak", "skipped", "disabled in settings")

                        if secret_leak_verdict is not None and secret_leak_verdict.ran and not secret_leak_verdict.ok:
                            # Still leaking after one corrective re-prompt — skip the
                            # expensive gate and fail now, the same severity class as
                            # an unresolved syntax error or a hallucinated dependency.
                            bad = ", ".join(f.kind for f in secret_leak_verdict.critical)
                            gate = GateResult(
                                passed=False,
                                signals_ran=1,
                                signals_passed=0,
                                tests=CheckResult(name="Tests"),
                                lint=CheckResult(name="Lint/typecheck"),
                                critic=CriticVerdict(),
                                thrash=ThrashVerdict(),
                                summary=f"Secret Leak Gate failed after one auto-corrective re-prompt: {bad}",
                                dependency_trust=dependency_trust_verdict if dependency_trust_verdict is not None else DependencyTrustVerdict(),
                                secret_leak=secret_leak_verdict,
                            )
                        else:
                            # DLE stage 7 — Test Quality Gate: only meaningful
                            # once the real tests are known to pass (a mutant
                            # "surviving" against an already-broken suite is
                            # noise, not a finding), so check that first with
                            # a direct, cheap run_tests() call before paying
                            # for the mutation pass itself.
                            test_quality_verdict: Optional[TestQualityVerdict] = None
                            if secrets.dle_test_quality:
                                ctx.dle_stage_event("testquality", "running")
                                try:
                                    baseline_tests = run_tests(workdir)
                                    if baseline_tests.ran and baseline_tests.passed:
                                        test_quality_verdict = run_test_quality_gate(
                                            workdir, diff, min_kill_rate=secrets.test_quality_min_kill_rate,
                                        )
                                        if test_quality_verdict.ran:
                                            if test_quality_verdict.passed:
                                                ctx.dle_stage_event(
                                                    "testquality", "pass",
                                                    f"{test_quality_verdict.mutants_killed}/{test_quality_verdict.mutants_generated} mutants killed",
                                                )
                                            else:
                                                weak = ", ".join(s.function for s in test_quality_verdict.survivors[:3])
                                                ctx.dle_stage_event("testquality", "fail", f"weak test coverage: {weak}")
                                        else:
                                            ctx.dle_stage_event(
                                                "testquality", "skipped",
                                                test_quality_verdict.skipped_reason or "nothing to mutate this turn",
                                            )
                                    else:
                                        ctx.dle_stage_event("testquality", "skipped", "no passing test suite to mutate against")
                                except Exception:
                                    logger.exception("test quality gate failed unexpectedly, skipping this signal")
                                    test_quality_verdict = None
                                    ctx.dle_stage_event("testquality", "skipped", "gate errored, skipped for this turn")
                            else:
                                ctx.dle_stage_event("testquality", "skipped", "disabled in settings")

                            gate = run_gate(
                                workdir, provider=provider, goal=goal, diff=diff,
                                invariants=[f"{i.title}: {i.body}" for i in ledger.invariants()],
                                recent_diffs=recent_diffs, min_signals_pass=secrets.verify_min_signals_pass,
                                telemetry=telemetry_verdict,
                                dependency_trust=dependency_trust_verdict,
                                secret_leak=secret_leak_verdict,
                                test_quality=test_quality_verdict,
                                critic_model=critic_model_override,
                            )

                            # Generalize the "one corrective re-prompt, then
                            # accept the verdict" pattern to the two Verify
                            # signals mechanical enough for it to help: a
                            # failing test or a lint/typecheck error. Skips
                            # entirely if the gate passed, or if it only
                            # failed on critic/thrash (judgment calls a
                            # re-prompt can't reliably fix) — in both cases
                            # build_qa_reprompt() returns "".
                            qa_reprompt = build_qa_reprompt(gate)
                            if qa_reprompt:
                                ctx.agent_line("[verify gate] fixable test/lint failure detected — issuing one corrective re-prompt")
                                corrective_prompt = f"{prompt}\n\n{qa_reprompt}"
                                runner.run(corrective_prompt, workdir, on_line=ctx.agent_line, model=agent_model_override)
                                effective_prompt = corrective_prompt
                                diff = checkpoints.diff_since(last_good)
                                gate = run_gate(
                                    workdir, provider=provider, goal=goal, diff=diff,
                                    invariants=[f"{i.title}: {i.body}" for i in ledger.invariants()],
                                    recent_diffs=recent_diffs, min_signals_pass=secrets.verify_min_signals_pass,
                                    telemetry=telemetry_verdict,
                                    dependency_trust=dependency_trust_verdict,
                                    secret_leak=secret_leak_verdict,
                                    critic_model=critic_model_override,
                                )

            recent_diffs.append(diff)
            recent_diffs = recent_diffs[-5:]
            last_gate = gate
            ctx.gate_event(turn, gate)

            if gate.passed:
                ledger.record_decision(turn, f"Turn {turn}: {goal[:80]}", gate.summary, tags=[chosen_agent])
                ctx.ledger_event("decision", goal[:80], turn)

                # DLE post-verify — Signed Turn Receipts: write the signed
                # attestation into the working tree BEFORE the checkpoint
                # commit below, so it lands in the exact same commit as the
                # diff it describes. Not a Verify signal (like Review Risk,
                # it never blocks a turn) — skipped in demo mode since
                # there's no real prompt/diff/gate to attest to.
                if not demo and secrets.dle_signed_receipts:
                    try:
                        receipt = build_receipt(
                            turn=turn, goal=goal, prompt=effective_prompt, diff=diff,
                            coding_agent=chosen_agent,
                            provider_name=provider.name if provider else "none",
                            model=(provider.fast_model or provider.default_model) if provider else "",
                            temperature=0.0,
                            gate=gate,
                        )
                        write_receipt(workdir, receipt)
                        ctx.receipt_event(receipt)
                    except Exception:
                        logger.exception("failed to write signed turn receipt, continuing without it")

                new_checkpoint = checkpoints.create(turn, goal[:150])
                last_good = new_checkpoint
                ctx.checkpoint_event(new_checkpoint, verified=True)

                # Review Risk only runs on turns that shipped — a rolled-back
                # turn needs nothing, it's already gone. This is the "what
                # should a human actually read" layer, not another pass/fail
                # gate: it never blocks a turn, it only ranks what already
                # passed. See verify/review_risk.py for why this is the bet.
                if not demo and secrets.dle_review_risk and diff.strip():
                    try:
                        dep_findings = list(gate.dependency_trust.suspicious) + list(gate.dependency_trust.critical)
                        secret_findings = list(gate.secret_leak.suspicious) + list(gate.secret_leak.critical)
                        test_quality_findings = list(gate.test_quality.survivors)
                        brief = build_review_brief(
                            workdir, turn, diff,
                            dependency_findings=dep_findings,
                            secret_findings=secret_findings,
                            test_quality_findings=test_quality_findings,
                        )
                        ctx.review_brief_event(brief)
                        # Feed this turn's outcome forward: the NEXT turn (not
                        # this one — it already shipped) escalates if this
                        # brief found a HIGH-risk file. See dle_risk_escalation.
                        escalate_next_turn = brief.high_count > 0
                        if brief.high_count:
                            ledger.record_decision(
                                turn, f"Turn {turn}: {brief.high_count} high-risk file(s) shipped",
                                brief.summary_line(), tags=["review-risk"],
                            )
                    except Exception:
                        logger.exception("review risk scoring failed, continuing without it")

                if not demo and git_ops.has_remote(workdir):
                    github_ship(workdir, mode="push")
                ctx.finish_phase(f"turn-{turn}", "Verified", success=True, stage="loop")
            else:
                reason = gate.critic.reasoning or gate.summary
                ledger.record_failure(turn, f"Turn {turn} failed: {goal[:60]}", f"{gate.summary} {reason}".strip(), tags=[chosen_agent])
                ctx.ledger_event("failure", goal[:80], turn)
                rollback_to(workdir, last_good)
                ctx.checkpoint_event(last_good, verified=False)
                ctx.finish_phase(f"turn-{turn}", "Rolled back", success=False, stage="loop")

            if should_distill(ledger, turn):
                distill(ledger, provider, turn)
                ctx.emit({"type": "distilled", "turn": turn})
            ledger.render_brain()

            ctx.start_phase(f"route-{turn}", "Planner", "Routing next turn…", stage="loop")
            if demo:
                turn_plan = TurnPlan(done=turn >= 3, follow_up=f"Polish for turn {turn + 1}", reason="Demo loop")
            else:
                turn_plan = generate_turn_plan(
                    provider, idea=idea, plan=plan, turn=turn, continuity_context=retrieval.context_block,
                    workdir_summary=workdir_summary(workdir, 25), verify_context=gate.to_context_block(),
                    last_follow_up=next_follow_up,
                )
            ctx.finish_phase(f"route-{turn}", turn_plan.reason, stage="loop")
            ctx.emit({"type": "turn_plan", "turn": turn, "done": turn_plan.done, "follow_up": turn_plan.follow_up, "reason": turn_plan.reason})

            if turn_plan.done:
                build_complete = True
                break
            next_follow_up = turn_plan.follow_up or next_follow_up

        if turn >= SAFETY_MAX_TURNS and not build_complete:
            logger.warning("Safety cap %s reached", SAFETY_MAX_TURNS)

        pr_url = None
        if build_complete and not demo and secrets.ship_mode == "pr" and git_ops.has_remote(workdir):
            ship_result = github_ship(workdir, mode="pr", title=brand.product_name, body=f"{brand.tagline}\n\n{plan}")
            pr_url = ship_result.get("url")

        if not demo:
            notify_completion(secrets, {
                "project_id": project.id, "run_id": run.id, "product_name": brand.product_name,
                "turns": turns_completed, "build_complete": build_complete, "pr_url": pr_url or "",
            })

        update_project(project.id, status="built" if build_complete else "error")
        result = {
            "idea": idea,
            "plan": plan,
            "brand": {"product_name": brand.product_name, "repo_slug": brand.repo_slug, "tagline": brand.tagline},
            "turns": turns_completed,
            "build_complete": build_complete,
            "agent": {"kind": agent_kind, "success": build_complete},
            "tracking": {"github_url": github_url or "", "linear_url": linear_url or ""},
            "verify": last_gate.to_dict() if last_gate else {},
            "checkpoints": len(checkpoints.list()),
            "ledger_stats": ledger.stats(),
            "pr_url": pr_url or "",
            "workdir": str(workdir),
        }
        status = "completed" if build_complete else "failed"
        update_run(run.id, status=status, phases=ctx.phases, result=result, current_phase="", finished_at=_ts())
        ctx.emit({"type": "complete", "status": status, "result": result})
        fire_webhook(
            secrets.webhook_url, "build.complete" if build_complete else "build.stopped",
            {"project_id": project.id, "run_id": run.id, "status": status, "build_complete": build_complete,
             "turns": turns_completed, "pr_url": pr_url or ""},
            secret=secrets.webhook_secret,
        )
        return result

    except Exception as e:
        logger.exception("supersonic run failed")
        update_run(run.id, status="failed", error=str(e), current_phase="", finished_at=_ts())
        ctx.emit({"type": "error", "message": str(e)})
        raise


def _fallback_plan(idea: str) -> str:
    return f"1. Scaffold MVP for: {idea}\n2. README + tests\n3. Core feature\n4. Polish"
