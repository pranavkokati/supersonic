"""Signed Turn Receipts — key management, build/write/verify round-trip, and
tamper detection (the actual point of signing them at all)."""

from __future__ import annotations

import json

import pytest

from supersonic.verify import receipts as rc
from supersonic.verify.critic import CriticVerdict
from supersonic.verify.dependency_trust import DependencyTrustVerdict
from supersonic.verify.gate import GateResult
from supersonic.verify.qa import CheckResult
from supersonic.verify.secret_leak import SecretLeakVerdict
from supersonic.verify.telemetry_gate import TelemetryVerdict
from supersonic.verify.test_quality import TestQualityVerdict
from supersonic.verify.thrash import ThrashVerdict


def _fake_gate(passed: bool = True) -> GateResult:
    return GateResult(
        passed=passed,
        signals_ran=2,
        signals_passed=2 if passed else 0,
        tests=CheckResult(name="Tests", ran=True, passed=passed, command="pytest -q"),
        lint=CheckResult(name="Lint/typecheck", ran=True, passed=passed, command="ruff check ."),
        critic=CriticVerdict(ran=True, satisfied=passed, reasoning="ok"),
        thrash=ThrashVerdict(),
        summary="2/2 verification signals passed (needed 2).",
        telemetry=TelemetryVerdict(),
        dependency_trust=DependencyTrustVerdict(),
        secret_leak=SecretLeakVerdict(),
        test_quality=TestQualityVerdict(),
    )


@pytest.fixture(autouse=True)
def _isolated_key_dir(tmp_path, monkeypatch):
    # Every test gets its own machine-local key directory so tests never
    # touch the real ~/.supersonic/keys/ and never interfere with each other.
    monkeypatch.setattr(rc, "CONFIG_DIR", tmp_path / "supersonic-home")
    yield


def test_load_or_create_signing_key_is_stable_across_calls():
    key1 = rc.load_or_create_signing_key()
    key2 = rc.load_or_create_signing_key()
    assert rc.public_key_hex(key1) == rc.public_key_hex(key2)


def test_private_key_file_has_restrictive_permissions():
    rc.load_or_create_signing_key()
    path = rc._private_key_path()
    assert path.exists()
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_diff_stat_counts_files_insertions_deletions():
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "+added line one\n"
        "+added line two\n"
        "-removed line\n"
        "diff --git a/y.py b/y.py\n"
        "--- a/y.py\n"
        "+++ b/y.py\n"
        "+another added line\n"
    )
    stat = rc._diff_stat(diff)
    assert stat == {"files_changed": 2, "insertions": 3, "deletions": 1}


def test_build_receipt_signs_and_round_trips_verification(tmp_path):
    gate = _fake_gate(passed=True)
    receipt = rc.build_receipt(
        turn=3, goal="add a login form", prompt="build turn 3...", diff="diff --git a/x.py b/x.py\n+ok\n",
        coding_agent="claude", provider_name="anthropic", model="claude-sonnet-5", temperature=0.0, gate=gate,
    )
    assert receipt.turn == 3
    assert receipt.signature
    assert receipt.payload["prompt_sha256"]
    assert receipt.payload["diff_sha256"]
    assert receipt.payload["gate"]["passed"] is True

    path = rc.write_receipt(tmp_path, receipt)
    assert path.exists()
    assert path.name == "turn-3.json"

    verification = rc.verify_receipt_file(path)
    assert verification.ok is True
    assert verification.turn == 3


def test_write_receipt_also_writes_a_pubkey_file(tmp_path):
    gate = _fake_gate()
    receipt = rc.build_receipt(
        turn=1, goal="g", prompt="p", diff="", coding_agent="claude",
        provider_name="anthropic", model="m", temperature=0.0, gate=gate,
    )
    rc.write_receipt(tmp_path, receipt)
    pubkey_file = tmp_path / rc.RECEIPTS_DIRNAME / rc.PUBKEY_FILENAME
    assert pubkey_file.exists()
    assert pubkey_file.read_text().strip() == receipt.payload["public_key"]


def test_verify_receipt_file_detects_tampering(tmp_path):
    gate = _fake_gate()
    receipt = rc.build_receipt(
        turn=5, goal="original goal", prompt="p", diff="d", coding_agent="claude",
        provider_name="anthropic", model="m", temperature=0.0, gate=gate,
    )
    path = rc.write_receipt(tmp_path, receipt)

    # Hand-edit the receipt on disk without re-signing — the exact failure
    # mode this feature exists to catch.
    data = json.loads(path.read_text())
    data["goal"] = "a completely different goal, edited after the fact"
    path.write_text(json.dumps(data))

    verification = rc.verify_receipt_file(path)
    assert verification.ok is False
    assert "signature invalid" in verification.reason


def test_verify_receipt_file_missing_signature_field(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"turn": 1, "public_key": "aa"}))
    verification = rc.verify_receipt_file(path)
    assert verification.ok is False
    assert "missing signature" in verification.reason


def test_verify_receipt_file_unreadable_json(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("not json at all {{{")
    verification = rc.verify_receipt_file(path)
    assert verification.ok is False
    assert "unreadable" in verification.reason


def test_verify_all_receipts_empty_when_no_receipts_dir(tmp_path):
    assert rc.verify_all_receipts(tmp_path) == []


def test_verify_all_receipts_covers_every_written_receipt(tmp_path):
    gate = _fake_gate()
    for turn in (1, 2, 3):
        receipt = rc.build_receipt(
            turn=turn, goal="g", prompt="p", diff="", coding_agent="claude",
            provider_name="anthropic", model="m", temperature=0.0, gate=gate,
        )
        rc.write_receipt(tmp_path, receipt)
    results = rc.verify_all_receipts(tmp_path)
    assert [r.turn for r in results] == [1, 2, 3]
    assert all(r.ok for r in results)


def test_a_receipt_signed_with_one_key_fails_against_a_swapped_public_key(tmp_path):
    """The core security property: you cannot forge a valid-looking receipt
    without the private key, even if you can freely edit the public_key
    field to point at a different keypair you *do* control."""
    gate = _fake_gate()
    receipt = rc.build_receipt(
        turn=1, goal="g", prompt="p", diff="", coding_agent="claude",
        provider_name="anthropic", model="m", temperature=0.0, gate=gate,
    )
    path = rc.write_receipt(tmp_path, receipt)

    forged_key = rc.Ed25519PrivateKey.generate()
    data = json.loads(path.read_text())
    data["public_key"] = rc.public_key_hex(forged_key)
    path.write_text(json.dumps(data))

    verification = rc.verify_receipt_file(path)
    assert verification.ok is False
