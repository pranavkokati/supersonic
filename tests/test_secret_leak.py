"""Secret Leak Gate — diff scanning, scoring, and Verify-gate integration.

No network calls involved (unlike Dependency Trust) — everything here is
pure regex/text scanning over synthetic diffs, so these tests need no
mocking at all.
"""

from __future__ import annotations

from pathlib import Path

from supersonic.verify.gate import GateResult, run_gate
from supersonic.verify.secret_leak import (
    SecretFinding,
    SecretLeakVerdict,
    _added_lines,
    _changed_blocks,
    _is_placeholder,
    run_secret_leak_gate,
    scan_file,
)


def _diff_for(path: str, added_lines):
    body = "\n".join(f"+{line}" for line in added_lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 111..222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"{body}\n"
    )


# --------------------------------------------------------------------------
# Diff parsing helpers
# --------------------------------------------------------------------------

def test_changed_blocks_splits_multi_file_diff():
    diff = _diff_for("a.py", ["x = 1"]) + _diff_for("b.py", ["y = 2"])
    blocks = _changed_blocks(diff)
    assert set(blocks.keys()) == {"a.py", "b.py"}


def test_added_lines_ignores_file_header_markers():
    block = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n+real_line = 1\n-old_line = 2\n"
    assert _added_lines(block) == ["real_line = 1"]


def test_is_placeholder_detects_known_conventions():
    assert _is_placeholder("your_api_key_here") is True
    assert _is_placeholder("xxxxxxxxxxxxxxxx") is True
    assert _is_placeholder("00000000000000000000") is True
    assert _is_placeholder("CHANGEME") is True
    assert _is_placeholder("a9f8Kj2mNpQ7rT4vXz1c") is False  # looks like a real opaque value


# --------------------------------------------------------------------------
# Critical (structured, high-specificity) patterns
# --------------------------------------------------------------------------

def test_scan_file_flags_aws_access_key():
    findings = scan_file("config.py", ['AWS_KEY = "AKIAABCDEFGHIJKLMNOP"'])
    assert len(findings) == 1
    assert findings[0].verdict == "critical"
    assert findings[0].kind == "AWS access key ID"


def test_scan_file_flags_pem_private_key_block():
    findings = scan_file("id_rsa", ["-----BEGIN RSA PRIVATE KEY-----"])
    assert len(findings) == 1
    assert findings[0].kind == "PEM private key block"


def test_scan_file_flags_github_token():
    findings = scan_file("script.sh", ['export GH_TOKEN="ghp_' + "a" * 36 + '"'])
    assert any(f.kind == "GitHub token" for f in findings)


def test_scan_file_flags_anthropic_key():
    findings = scan_file("client.py", ['ANTHROPIC_API_KEY = "sk-ant-' + "a" * 30 + '"'])
    assert any(f.kind == "Anthropic API key" for f in findings)


def test_scan_file_ignores_placeholder_values():
    findings = scan_file("config.py", ['AWS_KEY = "your_api_key_here"'])
    # "your_api_key_here" doesn't match the AWS regex shape anyway, and the
    # generic pattern must also treat it as a placeholder — no findings.
    assert findings == []


def test_scan_file_flags_new_env_file_regardless_of_content():
    findings = scan_file(".env", ["FOO=bar"])
    assert any(f.verdict == "env_file" for f in findings)


def test_scan_file_does_not_flag_env_example():
    findings = scan_file(".env.example", ["API_KEY=your_api_key_here"])
    assert not any(f.verdict == "env_file" for f in findings)


# --------------------------------------------------------------------------
# Suspicious (generic heuristic) pattern
# --------------------------------------------------------------------------

def test_scan_file_flags_generic_credential_shaped_assignment():
    findings = scan_file("settings.py", ['api_key = "aB3fG7hK9mN2pQ5rS8tU1vW4x"'])
    assert len(findings) == 1
    assert findings[0].verdict == "suspicious"


def test_scan_file_generic_pattern_ignores_short_values():
    findings = scan_file("settings.py", ['password = "short1"'])
    assert findings == []


# --------------------------------------------------------------------------
# run_secret_leak_gate — end to end
# --------------------------------------------------------------------------

def test_run_secret_leak_gate_empty_diff_is_noop(tmp_path):
    verdict = run_secret_leak_gate(tmp_path, "")
    assert verdict.ran is False
    assert verdict.ok is True


def test_run_secret_leak_gate_no_findings_is_noop(tmp_path):
    diff = _diff_for("app.py", ["def hello(): return 'world'"])
    verdict = run_secret_leak_gate(tmp_path, diff)
    assert verdict.ran is False


def test_run_secret_leak_gate_critical_finding_fails_and_builds_reprompt(tmp_path):
    diff = _diff_for("config.py", ['AWS_KEY = "AKIAABCDEFGHIJKLMNOP"'])
    verdict = run_secret_leak_gate(tmp_path, diff)
    assert verdict.ran is True
    assert verdict.ok is False
    assert len(verdict.critical) == 1
    assert "config.py" in verdict.reprompt
    assert "AWS access key ID" in verdict.reprompt


def test_run_secret_leak_gate_suspicious_finding_does_not_fail(tmp_path):
    diff = _diff_for("settings.py", ['api_key = "aB3fG7hK9mN2pQ5rS8tU1vW4x"'])
    verdict = run_secret_leak_gate(tmp_path, diff)
    assert verdict.ran is True
    assert verdict.ok is True  # suspicious never fails the turn outright
    assert len(verdict.suspicious) == 1
    assert verdict.reprompt == ""


def test_run_secret_leak_gate_new_env_file_is_critical(tmp_path):
    diff = _diff_for(".env", ["SECRET=whatever"])
    verdict = run_secret_leak_gate(tmp_path, diff)
    assert verdict.ran is True
    assert verdict.ok is False


def test_secret_finding_to_dict_roundtrip():
    f = SecretFinding(path="a.py", kind="AWS access key ID", verdict="critical", line_excerpt="AKIA…OP (AWS access key ID)")
    d = f.to_dict()
    assert d["path"] == "a.py"
    assert d["verdict"] == "critical"


def test_secret_leak_verdict_context_block_not_run():
    verdict = SecretLeakVerdict(ran=False)
    assert "Not run" in verdict.to_context_block()


def test_secret_leak_verdict_context_block_pass():
    verdict = SecretLeakVerdict(ran=True, ok=True)
    assert "PASS" in verdict.to_context_block()


# --------------------------------------------------------------------------
# Verify gate integration — backward compatibility + new hard-fail behavior
# --------------------------------------------------------------------------

def test_gate_backward_compatible_when_secret_leak_not_passed(tmp_path):
    # Every caller that predates this signal never passes secret_leak= at
    # all; this must behave exactly as before it existed.
    result = run_gate(
        tmp_path, provider=None, goal="test", diff="", invariants=[], recent_diffs=[], min_signals_pass=3,
    )
    assert isinstance(result, GateResult)
    assert result.secret_leak.ran is False


def test_gate_fails_outright_on_critical_secret_regardless_of_other_signals():
    critical_finding = SecretFinding(path="config.py", kind="AWS access key ID", verdict="critical")
    verdict = SecretLeakVerdict(ran=True, ok=False, critical=[critical_finding])
    result = run_gate(
        Path("/tmp/does-not-matter"), provider=None, goal="test", diff="", invariants=[], recent_diffs=[],
        min_signals_pass=1, secret_leak=verdict,
    )
    assert result.passed is False
    assert "Secret Leak Gate failed" in result.summary


def test_gate_passes_when_secret_leak_ok():
    verdict = SecretLeakVerdict(ran=True, ok=True, suspicious=[
        SecretFinding(path="settings.py", kind="credential-shaped assignment", verdict="suspicious"),
    ])
    result = run_gate(
        Path("/tmp/does-not-matter"), provider=None, goal="test", diff="", invariants=[], recent_diffs=[],
        min_signals_pass=1, secret_leak=verdict,
    )
    # No other signals ran either, but secret_leak ran and passed, so it
    # counts as the one signal that must be satisfied.
    assert result.passed is True
