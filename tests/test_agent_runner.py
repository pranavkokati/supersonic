"""CodingAgentRunner._command() — Risk-Aware Model Escalation's lever on the
coding-agent CLI itself. Verifies the --model flag is appended for every
agent kind whose flag support was confirmed (claude, codex, opencode,
cursor-agent, aider), omitted when no model is requested, and NOT appended
on cursor's unverified fallback command shape."""

from __future__ import annotations

from unittest.mock import patch

from supersonic.agents.runner import CodingAgentRunner
from supersonic.config import UserSecrets


def _runner(kind: str) -> CodingAgentRunner:
    return CodingAgentRunner(kind, UserSecrets())


def test_no_model_override_leaves_every_command_unchanged():
    for kind in ("claude", "codex", "opencode", "aider"):
        cmd = _runner(kind)._command(prompt_file=None, prompt="do the thing")
        assert "--model" not in cmd


def test_claude_appends_model_flag_when_escalated():
    cmd = _runner("claude")._command(prompt_file=None, prompt="do the thing", model="opus")
    assert cmd[:2] == ["claude", "-p"]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "opus"


def test_codex_appends_model_flag_when_escalated(monkeypatch):
    monkeypatch.setattr("supersonic.agents.runner.shutil.which", lambda b: "/usr/bin/codex" if b == "codex" else None)
    cmd = _runner("codex")._command(prompt_file=None, prompt="do the thing", model="gpt-5.3-codex")
    assert cmd[0] == "codex"
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "gpt-5.3-codex"


def test_opencode_appends_model_flag_when_escalated(tmp_path):
    prompt_file = tmp_path / "SONIC_PROMPT.md"
    prompt_file.write_text("hi")
    cmd = _runner("opencode")._command(prompt_file=prompt_file, prompt="do the thing", model="anthropic/claude-opus-4-8")
    assert cmd[:2] == ["opencode", "run"]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "anthropic/claude-opus-4-8"


def test_aider_appends_model_flag_when_escalated():
    cmd = _runner("aider")._command(prompt_file=None, prompt="do the thing", model="anthropic/claude-opus-4-8")
    assert cmd[0] == "aider"
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "anthropic/claude-opus-4-8"


def test_cursor_agent_binary_appends_model_flag_when_escalated(monkeypatch):
    monkeypatch.setattr("supersonic.agents.runner.shutil.which", lambda b: "/usr/bin/cursor-agent" if b == "cursor-agent" else None)
    cmd = _runner("cursor")._command(prompt_file=None, prompt="do the thing", model="gpt-5")
    assert cmd[0] == "cursor-agent"
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "gpt-5"


def test_cursor_fallback_command_never_gets_an_unverified_model_flag(monkeypatch):
    # cursor-agent binary not found -> falls back to the "cursor agent ..." shape,
    # whose --model support was never confirmed, so it must not be guessed at.
    monkeypatch.setattr("supersonic.agents.runner.shutil.which", lambda b: None)
    cmd = _runner("cursor")._command(prompt_file=None, prompt="do the thing", model="gpt-5")
    assert cmd == ["cursor", "agent", "-p", "do the thing"]
    assert "--model" not in cmd


def test_unknown_agent_kind_ignores_model_override():
    cmd = _runner("not-a-real-agent")._command(prompt_file=None, prompt="x", model="opus")
    assert "--model" not in cmd


def test_run_passes_model_through_to_command(monkeypatch, tmp_path):
    captured = {}

    def fake_run_streaming(cmd, workdir, env, on_line=None, timeout=1800, line_mapper=None):
        captured["cmd"] = cmd
        from supersonic.agents.runner import AgentResult

        return AgentResult(agent=cmd[0], success=True, output="", command=" ".join(cmd))

    with patch("supersonic.agents.runner._run_streaming", side_effect=fake_run_streaming):
        _runner("claude").run("do the thing", tmp_path, model="opus")

    assert "--model" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "opus"
