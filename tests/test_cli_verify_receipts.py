"""`sonic verify-receipts` — the CLI-facing entrypoint for checking Signed
Turn Receipts, exercised through Typer's CliRunner so it's covered the same
way a real user invocation would go (argument parsing, exit codes, table
output), not just the underlying receipts.py functions directly."""

from __future__ import annotations

from typer.testing import CliRunner

from supersonic.cli import app
from supersonic.verify import receipts as rc
from supersonic.verify.critic import CriticVerdict
from supersonic.verify.gate import GateResult
from supersonic.verify.qa import CheckResult
from supersonic.verify.thrash import ThrashVerdict

runner = CliRunner()


def _fake_gate() -> GateResult:
    return GateResult(
        passed=True, signals_ran=1, signals_passed=1,
        tests=CheckResult(name="Tests", ran=True, passed=True),
        lint=CheckResult(name="Lint/typecheck"),
        critic=CriticVerdict(), thrash=ThrashVerdict(), summary="1/1 passed",
    )


def test_verify_receipts_reports_no_receipts_found(tmp_path):
    result = runner.invoke(app, ["verify-receipts", str(tmp_path)])
    assert result.exit_code == 0
    assert "No receipts found" in result.stdout


def test_verify_receipts_exits_zero_for_a_clean_project(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "CONFIG_DIR", tmp_path / "supersonic-home")
    receipt = rc.build_receipt(
        turn=1, goal="g", prompt="p", diff="", coding_agent="claude",
        provider_name="anthropic", model="m", temperature=0.0, gate=_fake_gate(),
    )
    workdir = tmp_path / "proj"
    rc.write_receipt(workdir, receipt)

    result = runner.invoke(app, ["verify-receipts", str(workdir)])
    assert result.exit_code == 0
    assert "verified" in result.stdout


def test_verify_receipts_exits_nonzero_when_a_receipt_is_tampered(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(rc, "CONFIG_DIR", tmp_path / "supersonic-home")
    receipt = rc.build_receipt(
        turn=1, goal="g", prompt="p", diff="", coding_agent="claude",
        provider_name="anthropic", model="m", temperature=0.0, gate=_fake_gate(),
    )
    workdir = tmp_path / "proj"
    path = rc.write_receipt(workdir, receipt)
    data = json.loads(path.read_text())
    data["goal"] = "tampered"
    path.write_text(json.dumps(data))

    result = runner.invoke(app, ["verify-receipts", str(workdir)])
    assert result.exit_code == 1
