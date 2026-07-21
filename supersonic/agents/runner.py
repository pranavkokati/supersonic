"""Coding agent adapters — Claude Code, Codex, OpenCode, Cursor Agent, Aider.

Bring-your-own-agent: Supersonic doesn't lock you into one coding-agent CLI.
Any of these can be the default backend for a project.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from supersonic.config import AgentKind, UserSecrets

logger = logging.getLogger(__name__)

LineCallback = Callable[[str], None]


@dataclass
class AgentResult:
    agent: str
    success: bool
    output: str
    command: str


def _cursor_stream_line(raw: str) -> Optional[str]:
    line = raw.strip()
    if not line:
        return None
    if not line.startswith("{"):
        return line
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return line
    if not isinstance(obj, dict):
        return line

    typ = str(obj.get("type") or obj.get("event") or "")
    if typ in ("assistant", "message", "text", "content", "result"):
        content = obj.get("content") or obj.get("text") or obj.get("message") or obj.get("result")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [p.get("text", p) if isinstance(p, dict) else str(p) for p in content]
            joined = "".join(str(p) for p in parts if p).strip()
            if joined:
                return joined

    if typ in ("tool_call", "tool_use", "tool", "function_call"):
        name = obj.get("name") or obj.get("tool") or obj.get("tool_name") or "tool"
        return f"▸ {name}"

    delta = obj.get("delta")
    if isinstance(delta, str) and delta.strip():
        return delta.strip()
    if isinstance(delta, dict):
        chunk = delta.get("content") or delta.get("text")
        if isinstance(chunk, str) and chunk.strip():
            return chunk.strip()

    subtype = obj.get("subtype") or obj.get("status")
    if subtype and typ:
        return f"· {typ}:{subtype}"
    return None


def _run_streaming(
    cmd: List[str],
    workdir: Path,
    env: Dict[str, str],
    on_line: Optional[LineCallback] = None,
    timeout: int = 1800,
    line_mapper: Optional[Callable[[str], Optional[str]]] = None,
) -> AgentResult:
    agent = cmd[0]
    lines: List[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            env={**os.environ, **env},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        def _reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                out_line = line_mapper(line) if line_mapper else line
                if out_line is None:
                    continue
                lines.append(out_line)
                if on_line:
                    on_line(out_line)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            return AgentResult(
                agent=agent, success=False, output=f"Agent timed out after {timeout}s.", command=" ".join(cmd)
            )
        t.join(timeout=2)
        out = "\n".join(lines)[-12000:]
        return AgentResult(agent=agent, success=proc.returncode == 0, output=out, command=" ".join(cmd))
    except FileNotFoundError:
        msg = f"CLI not found: {agent}. Install it and ensure it's on PATH."
        if on_line:
            on_line(msg)
        return AgentResult(agent=agent, success=False, output=msg, command=" ".join(cmd))


def _write_prompt(workdir: Path, prompt: str) -> Path:
    p = workdir / "SONIC_PROMPT.md"
    p.write_text(prompt)
    return p


class CodingAgentRunner:
    """Runs one coding-agent CLI to completion on a prompt."""

    def __init__(self, kind: AgentKind, secrets: UserSecrets):
        self.kind = kind
        self.secrets = secrets

    def run(
        self, prompt: str, workdir: Path, on_line: Optional[LineCallback] = None, model: Optional[str] = None,
    ) -> AgentResult:
        """`model`, when set, is passed straight through to the CLI's own
        --model flag (Risk-Aware Model Escalation's lever on the coding agent
        itself — see config.py's escalation_model_* fields). None/"" means
        "run at whatever model this CLI is configured to use by default"."""
        workdir.mkdir(parents=True, exist_ok=True)
        prompt_file = _write_prompt(workdir, prompt)
        env = self._env()
        cmd = self._command(prompt_file, prompt, model=model)
        if on_line:
            on_line(f"$ {' '.join(cmd)}")
        mapper = _cursor_stream_line if self.kind == "cursor" else None

        # PTY-native execution (dle_pty_supervision): the child gets a real
        # terminal instead of a plain pipe. POSIX only, and best-effort —
        # any failure to even start in PTY mode falls straight back to the
        # plain-subprocess path rather than failing the turn over it. See
        # agents/pty_runner.py for exactly what this does and doesn't do.
        if getattr(self.secrets, "dle_pty_supervision", False):
            from supersonic.agents.pty_runner import PTY_AVAILABLE, PTYUnavailableError, run_in_pty

            if PTY_AVAILABLE:
                try:
                    return run_in_pty(cmd, workdir, env, on_line=on_line, line_mapper=mapper, timeout=1800)
                except PTYUnavailableError:
                    pass  # fall through to the plain-subprocess path below
                except Exception:
                    logger.exception("PTY execution failed unexpectedly, falling back to plain subprocess")

        return _run_streaming(cmd, workdir, env, on_line=on_line, line_mapper=mapper, timeout=1800)

    def _env(self) -> Dict[str, str]:
        e: Dict[str, str] = {}
        if self.secrets.openai_api_key:
            e["OPENAI_API_KEY"] = self.secrets.openai_api_key
        if self.secrets.anthropic_api_key:
            e["ANTHROPIC_API_KEY"] = self.secrets.anthropic_api_key
        return e

    def _command(self, prompt_file: Path, prompt: str, model: Optional[str] = None) -> List[str]:
        text = prompt[:8000]
        model = (model or "").strip()
        if self.kind == "codex":
            if shutil.which("codex"):
                cmd = ["codex", "exec", "--full-auto", text]
            elif shutil.which("npx"):
                cmd = ["npx", "-y", "@openai/codex", "exec", "--full-auto", text]
            else:
                cmd = ["codex", "exec", text]
            # Confirmed: `codex exec --model/-m <name>` (developers.openai.com/codex/cli/reference).
            return cmd + (["--model", model] if model else [])
        if self.kind == "claude":
            cmd = ["claude", "-p", text, "--dangerously-skip-permissions"]
            # Confirmed: `claude -p "..." --model <name|alias>` (code.claude.com/docs/en/model-config).
            return cmd + (["--model", model] if model else [])
        if self.kind == "opencode":
            cmd = ["opencode", "run", str(prompt_file)]
            # Confirmed: `opencode run --model/-m provider/model` (open-code.ai/en/docs/cli).
            return cmd + (["--model", model] if model else [])
        if self.kind == "cursor":
            if shutil.which("cursor-agent"):
                cmd = [
                    "cursor-agent", "-p", "--force", "--trust",
                    "--output-format", "stream-json", "--stream-partial-output", text,
                ]
                # Confirmed for the cursor-agent binary specifically: `cursor-agent
                # -p "..." --model <name>` (cursor.com/docs/cli/reference/parameters).
                return cmd + (["--model", model] if model else [])
            # Fallback path uses a different, unverified command shape ("cursor
            # agent ...") — deliberately not appending an unconfirmed flag here.
            return ["cursor", "agent", "-p", text]
        if self.kind == "aider":
            cmd = ["aider", "--yes-always", "--no-check-update", "--message", text]
            # Confirmed: aider routes through LiteLLM, `--model provider/model-id`.
            return cmd + (["--model", model] if model else [])
        return ["echo", f"unknown agent kind: {self.kind}"]


def available_agents() -> List[Dict[str, object]]:
    checks = [
        ("claude", ["claude"]),
        ("codex", ["codex", "npx"]),
        ("opencode", ["opencode"]),
        ("cursor", ["cursor-agent", "cursor"]),
        ("aider", ["aider"]),
    ]
    out = []
    for kind, bins in checks:
        found = any(shutil.which(b) for b in bins)
        out.append({"id": kind, "available": found, "bins": bins, "label": kind.title()})
    return out
