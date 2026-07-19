"""Patch-diff mode — optional unified-diff-only turn execution.

The default turn path (`CodingAgentRunner.run`) lets the coding agent edit
files in the workdir directly — whatever the CLI wrote to disk is what gets
diffed and verified. That's simple and works with every backend, but it
means every turn pays for however much the agent decides to touch, and the
diff we verify is a *side effect* of the run rather than something we asked
for directly.

Patch-diff mode asks the agent instead to respond with a single unified git
diff (no direct file edits), which we then apply ourselves with `git apply`.
When it works, the diff is smaller, more reviewable, and cheaper to check
before it ever touches disk.

This is a real fallback chain, not a "TODO: handle failure" stub:

  1. Prompt the agent for a diff-only response.
  2. Try to extract a unified diff from its output and `git apply --check` it.
  3. If that fails, re-prompt the agent EXACTLY ONCE, showing it the precise
     `git apply` stderr, and ask for a corrected diff against the same base.
  4. Try to extract + apply the corrected diff.
  5. If it still doesn't apply, give up on patch mode *for this turn only*
     and fall back to the existing full-file-rewrite path — i.e. the caller
     re-runs the same goal through `CodingAgentRunner.run` unmodified, and
     the agent edits the working tree directly like it always has. Patch
     mode failing never blocks a turn; it just costs one extra agent call.

Nothing here mutates git history — `git apply` only touches the working
tree, exactly like a direct file edit would, so the existing
Checkpoint/Verify/Rollback contract (commit-and-tag on pass, hard-reset on
fail) is unaffected either way.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from supersonic.agents.runner import AgentResult, CodingAgentRunner, LineCallback

logger = logging.getLogger(__name__)

DIFF_INSTRUCTIONS = """
## Output format — patch-diff mode
Respond with ONLY a single unified git diff (the output of `git diff`) that
applies cleanly to the current working tree with `git apply`. Do not write
any prose, explanation, or markdown fences — just the raw diff, starting
with `diff --git`. Do not edit files directly; the diff is the entire
response.
"""

_DIFF_FENCE_RE = re.compile(r"```(?:diff|patch)?\s*\n(.*?)```", re.DOTALL)
_DIFF_START_RE = re.compile(r"^diff --git .*$", re.MULTILINE)


@dataclass
class PatchModeResult:
    used_patch_mode: bool
    applied: bool
    attempts: int
    agent_result: AgentResult
    fallback_reason: str = ""


def extract_diff(text: str) -> Optional[str]:
    """Pull a unified diff out of an agent's raw text response, if present."""
    if not text or not text.strip():
        return None
    fence = _DIFF_FENCE_RE.search(text)
    if fence:
        candidate = fence.group(1).strip()
        if "diff --git" in candidate or candidate.startswith(("---", "+++")):
            return candidate + "\n"
    match = _DIFF_START_RE.search(text)
    if match:
        return text[match.start():].strip() + "\n"
    return None


def try_git_apply(diff: str, workdir: Path, *, check_only: bool = False) -> subprocess.CompletedProcess:
    args = ["apply", "--whitespace=fix"]
    if check_only:
        args.append("--check")
    try:
        return subprocess.run(
            ["git", *args, "-"], cwd=str(workdir), input=diff, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(args=["git", *args], returncode=1, stdout="", stderr=f"git apply timed out: {e}")


def _attempt_apply(agent_result: AgentResult, workdir: Path) -> tuple[bool, str]:
    """Try to extract + apply a diff from one agent response.
    Returns (applied, error_text_for_reprompt_or_empty)."""
    diff = extract_diff(agent_result.output)
    if not diff:
        return False, "Your response did not contain a unified diff (nothing matched `diff --git ...`)."

    check = try_git_apply(diff, workdir, check_only=True)
    if check.returncode != 0:
        return False, f"`git apply --check` reported this error:\n```\n{check.stderr.strip()[:2000]}\n```"

    apply_res = try_git_apply(diff, workdir)
    if apply_res.returncode != 0:
        # --check passed but the real apply failed anyway (e.g. a racing on-disk
        # change) — surface it the same way so the re-prompt has something concrete.
        return False, f"`git apply` failed even though `--check` passed:\n```\n{apply_res.stderr.strip()[:2000]}\n```"

    return True, ""


def run_patch_diff_turn(
    runner: CodingAgentRunner, prompt: str, workdir: Path, on_line: Optional[LineCallback] = None,
    model: Optional[str] = None,
) -> PatchModeResult:
    """Attempt one turn in patch-diff mode. Never raises — worst case, applied=False
    and the caller falls back to the normal full-file-rewrite path.

    Fallback chain: prompt -> try apply -> (on any failure) ONE stricter
    re-prompt showing the exact error -> try apply again -> give up.

    `model` is passed straight through to every runner.run() call below —
    Risk-Aware Model Escalation's lever, same as the non-patch-mode path."""
    diff_prompt = prompt + "\n" + DIFF_INSTRUCTIONS
    agent_result = runner.run(diff_prompt, workdir, on_line=on_line, model=model)

    applied, error_text = _attempt_apply(agent_result, workdir)
    if applied:
        return PatchModeResult(used_patch_mode=True, applied=True, attempts=1, agent_result=agent_result)

    logger.info("patch-diff mode: first attempt failed (%s), re-prompting once", error_text.splitlines()[0][:120])
    reprompt = (
        f"{diff_prompt}\n\n"
        "## Your previous response could not be applied\n"
        f"{error_text}\n\n"
        "Return a corrected unified diff that applies cleanly against the CURRENT working "
        "tree. Output ONLY the diff, nothing else."
    )
    agent_result2 = runner.run(reprompt, workdir, on_line=on_line, model=model)
    applied2, error_text2 = _attempt_apply(agent_result2, workdir)
    if applied2:
        return PatchModeResult(used_patch_mode=True, applied=True, attempts=2, agent_result=agent_result2)

    return PatchModeResult(
        used_patch_mode=True, applied=False, attempts=2, agent_result=agent_result2,
        fallback_reason=f"still failed after one re-prompt: {error_text2.splitlines()[0][:300]}",
    )
