"""Dependency Trust Gate — manifest/install-command parsing, scoring, and
Verify-gate integration. No real network calls: registry lookups are
monkeypatched at the `_check_pypi`/`_check_npm` boundary.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import supersonic.verify.dependency_trust as dt
from supersonic.verify.dependency_trust import (
    DependencyTrustVerdict,
    PackageFinding,
    _age_days,
    _collect_candidates,
    _extract_install_command_packages,
    _extract_npm_packages,
    _extract_python_packages,
    run_dependency_trust,
)
from supersonic.verify.gate import GateResult, run_gate


def _requirements_diff(added_lines):
    body = "\n".join(f"+{line}" for line in added_lines)
    return (
        "diff --git a/requirements.txt b/requirements.txt\n"
        "index 111..222 100644\n"
        "--- a/requirements.txt\n"
        "+++ b/requirements.txt\n"
        f"{body}\n"
    )


def _package_json_diff(added_lines):
    body = "\n".join(f"+{line}" for line in added_lines)
    return (
        "diff --git a/package.json b/package.json\n"
        "index 111..222 100644\n"
        "--- a/package.json\n"
        "+++ b/package.json\n"
        f"{body}\n"
    )


# --------------------------------------------------------------------------
# Manifest parsing
# --------------------------------------------------------------------------

def test_extract_python_packages_requirements_txt():
    added = ["requests==2.31.0", "# a comment", "", "-e git+https://example.com/x.git", "click>=8.0"]
    names = _extract_python_packages("requirements.txt", added)
    assert names == ["requests", "click"]


def test_extract_python_packages_pyproject_poetry_style():
    added = ['requests = "^2.31.0"', 'not-a-dep-line', 'black = ">=24.0"']
    names = _extract_python_packages("pyproject.toml", added)
    assert names == ["requests", "black"]


def test_extract_python_packages_pyproject_pep621_list_style():
    added = ['    "requests>=2.31.0",', '    "click",']
    names = _extract_python_packages("pyproject.toml", added)
    assert names == ["requests", "click"]


def test_extract_python_packages_pipfile():
    added = ['requests = "*"', 'numpy = "==1.26.0"']
    names = _extract_python_packages("Pipfile", added)
    assert names == ["requests", "numpy"]


def test_extract_python_packages_ignores_non_manifest_file():
    assert _extract_python_packages("README.md", ["requests==2.31.0"]) == []


def test_extract_npm_packages_package_json():
    added = ['"axios": "^1.6.0",', '"name": "my-app",', '"@scope/pkg": "~2.0.0"']
    names = _extract_npm_packages("package.json", added)
    assert names == ["axios", "@scope/pkg"]


def test_extract_npm_packages_ignores_other_files():
    assert _extract_npm_packages("requirements.txt", ['"axios": "^1.6.0",']) == []


# --------------------------------------------------------------------------
# Install-command scanning
# --------------------------------------------------------------------------

def test_extract_install_command_pip():
    diff = "+pip install some-totally-fake-package==1.0\n+echo done\n"
    found = _extract_install_command_packages(diff)
    assert found["pypi"] == ["some-totally-fake-package"]
    assert found["npm"] == []


def test_extract_install_command_npm():
    diff = "+npm install left-pad-but-not-really --save\n"
    found = _extract_install_command_packages(diff)
    assert found["npm"] == ["left-pad-but-not-really"]


def test_extract_install_command_ignores_context_lines():
    diff = " pip install real-package\n+pip install newly-added-package\n"
    found = _extract_install_command_packages(diff)
    assert found["pypi"] == ["newly-added-package"]


def test_collect_candidates_combines_manifest_and_install_command():
    diff = _requirements_diff(["totally-hallucinated-pkg==1.0"]) + "+pip install another-fake-one\n"
    candidates = _collect_candidates(diff)
    names = {c[0] for c in candidates}
    assert "totally-hallucinated-pkg" in names
    assert "another-fake-one" in names


# --------------------------------------------------------------------------
# Age computation
# --------------------------------------------------------------------------

def test_age_days_parses_iso_with_z():
    assert _age_days("2000-01-01T00:00:00.000000Z") is not None
    assert _age_days("2000-01-01T00:00:00.000000Z") > 1000  # long-published, definitely old


def test_age_days_none_for_missing_or_malformed():
    assert _age_days(None) is None
    assert _age_days("not-a-date") is None


# --------------------------------------------------------------------------
# run_dependency_trust — end to end with mocked registry
# --------------------------------------------------------------------------

def test_run_dependency_trust_no_candidates_is_noop(tmp_path):
    verdict = run_dependency_trust(tmp_path, "diff --git a/README.md b/README.md\n+hello\n")
    assert verdict.ran is False
    assert verdict.ok is True


def test_run_dependency_trust_empty_diff_is_noop(tmp_path):
    verdict = run_dependency_trust(tmp_path, "")
    assert verdict.ran is False


def test_run_dependency_trust_flags_nonexistent_package(tmp_path):
    diff = _requirements_diff(["totally-hallucinated-pkg-xyz==1.0"])

    def fake_check_pypi(name, client):
        return PackageFinding(
            name=name, ecosystem="pypi", manifest="", verdict="nonexistent",
            reason=f"'{name}' does not exist on PyPI.",
        )

    with patch.object(dt, "_check_pypi", side_effect=fake_check_pypi):
        verdict = run_dependency_trust(tmp_path, diff)

    assert verdict.ran is True
    assert verdict.ok is False
    assert len(verdict.critical) == 1
    assert verdict.critical[0].name == "totally-hallucinated-pkg-xyz"
    assert "slopsquatting" in verdict.reprompt
    assert "totally-hallucinated-pkg-xyz" in verdict.reprompt


def test_run_dependency_trust_flags_suspicious_but_does_not_fail(tmp_path):
    diff = _requirements_diff(["brand-new-pkg==0.0.1"])

    def fake_check_pypi(name, client):
        return PackageFinding(
            name=name, ecosystem="pypi", manifest="", verdict="suspicious",
            reason="published 3 day(s) ago with only 1 release(s) on PyPI.",
            age_days=3, release_count=1,
        )

    with patch.object(dt, "_check_pypi", side_effect=fake_check_pypi):
        verdict = run_dependency_trust(tmp_path, diff)

    assert verdict.ran is True
    assert verdict.ok is True  # suspicious never fails the turn outright
    assert len(verdict.suspicious) == 1
    assert verdict.reprompt == ""


def test_run_dependency_trust_trusted_package_produces_no_findings(tmp_path):
    diff = _requirements_diff(["requests==2.31.0"])

    def fake_check_pypi(name, client):
        return PackageFinding(name=name, ecosystem="pypi", manifest="", verdict="trusted", age_days=3000, release_count=120)

    with patch.object(dt, "_check_pypi", side_effect=fake_check_pypi):
        verdict = run_dependency_trust(tmp_path, diff)

    assert verdict.ran is True
    assert verdict.ok is True
    assert len(verdict.trusted) == 1
    assert verdict.critical == []
    assert verdict.suspicious == []


def test_run_dependency_trust_network_failure_is_excluded_not_penalized(tmp_path):
    diff = _requirements_diff(["some-package==1.0"])

    with patch.object(dt, "_check_pypi", side_effect=lambda name, client: None):
        verdict = run_dependency_trust(tmp_path, diff)

    assert verdict.ran is True
    assert verdict.ok is True  # unresolved must never count as a failure
    assert "some-package" in verdict.unresolved
    assert verdict.critical == []


def test_run_dependency_trust_npm_scoped_package(tmp_path):
    diff = _package_json_diff(['"@fake-scope/hallucinated": "^1.0.0",'])

    def fake_check_npm(name, client):
        return PackageFinding(name=name, ecosystem="npm", manifest="", verdict="nonexistent", reason="does not exist")

    with patch.object(dt, "_check_npm", side_effect=fake_check_npm):
        verdict = run_dependency_trust(tmp_path, diff)

    assert verdict.ran is True
    assert verdict.ok is False
    assert verdict.critical[0].name == "@fake-scope/hallucinated"


def test_npm_url_encodes_scoped_package_slash():
    assert dt._npm_url("@scope/pkg") == "https://registry.npmjs.org/@scope%2Fpkg"
    assert dt._npm_url("axios") == "https://registry.npmjs.org/axios"


# --------------------------------------------------------------------------
# Verify gate integration — backward compatibility + new hard-fail behavior
# --------------------------------------------------------------------------

def test_gate_backward_compatible_when_dependency_trust_not_passed(tmp_path):
    # Every existing caller (and every existing test) never passes
    # dependency_trust= at all; this must behave exactly as before.
    result = run_gate(
        tmp_path, provider=None, goal="test", diff="", invariants=[], recent_diffs=[], min_signals_pass=3,
    )
    assert isinstance(result, GateResult)
    assert result.dependency_trust.ran is False


def test_gate_fails_outright_on_nonexistent_dependency_regardless_of_other_signals():
    critical_finding = PackageFinding(name="fake-pkg", ecosystem="pypi", manifest="requirements.txt", verdict="nonexistent")
    verdict = DependencyTrustVerdict(ran=True, ok=False, critical=[critical_finding])
    result = run_gate(
        Path("/tmp/does-not-matter"), provider=None, goal="test", diff="", invariants=[], recent_diffs=[],
        min_signals_pass=1, dependency_trust=verdict,
    )
    assert result.passed is False
    assert "Dependency Trust Gate failed" in result.summary


def test_gate_passes_when_dependency_trust_ok():
    verdict = DependencyTrustVerdict(ran=True, ok=True, trusted=[
        PackageFinding(name="requests", ecosystem="pypi", manifest="requirements.txt", verdict="trusted"),
    ])
    result = run_gate(
        Path("/tmp/does-not-matter"), provider=None, goal="test", diff="", invariants=[], recent_diffs=[],
        min_signals_pass=1, dependency_trust=verdict,
    )
    # No other signals ran either, but dependency_trust ran and passed, so it
    # counts as the one signal that must be satisfied.
    assert result.passed is True
